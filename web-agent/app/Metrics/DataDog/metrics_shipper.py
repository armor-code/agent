#!/usr/bin/env python3
"""
ArmorCode Metrics Shipper to DataDog

Ships metrics from local JSON files to DataDog with:
- Duplicate prevention using position tracking
- File rotation handling
- Multi-worker support
- Graceful shutdown
- Health check endpoint
"""

import os
import json
import time
import signal
import logging
import glob
import sys
from pathlib import Path
from datadog import initialize, api
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Event
from typing import Dict, Tuple, List, Optional
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('metrics-shipper')


# ============================================================================
# Configuration
# ============================================================================

class ShipperConfig:
    """Load and validate configuration from environment variables."""

    def __init__(self):
        self.datadog_api_key = os.getenv('DATADOG_API_KEY')
        self.datadog_app_key = os.getenv('DATADOG_APP_KEY')
        self.datadog_site = os.getenv('DATADOG_SITE', 'datadoghq.com')

        self.metrics_dir = os.getenv('METRICS_DIR', '/tmp/armorcode/metrics')
        self.metrics_pattern = os.getenv('METRICS_PATTERN', 'metrics*.json')

        self.batch_size = int(os.getenv('BATCH_SIZE', '100'))
        self.batch_timeout_sec = int(os.getenv('BATCH_TIMEOUT_SEC', '5'))

        self.health_check_port = int(os.getenv('HEALTH_CHECK_PORT', '9090'))
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')

        self.state_file = os.getenv('STATE_FILE', '/var/lib/armorcode/shipper_state.json')

        # Validate required settings
        self.validate()

    def validate(self):
        """Validate required configuration."""
        if not self.datadog_api_key:
            raise ValueError("DATADOG_API_KEY environment variable is required")
        if not self.datadog_app_key:
            raise ValueError("DATADOG_APP_KEY environment variable is required")

        # Set log level
        log_level = getattr(logging, self.log_level.upper(), logging.INFO)
        logger.setLevel(log_level)

        logger.info(f"Configuration loaded: metrics_dir={self.metrics_dir}, "
                   f"pattern={self.metrics_pattern}, batch_size={self.batch_size}")


# ============================================================================
# Position Tracker
# ============================================================================

class FilePositionTracker:
    """
    Track read positions per file with rotation detection.

    Prevents duplicate metric submissions by tracking:
    - File path
    - Inode (to detect rotation)
    - Read position
    """

    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state: Dict[str, Dict] = {}
        self.load_state()

    def load_state(self):
        """Load state from disk."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.state = data.get('files', {})
                    logger.info(f"Loaded state for {len(self.state)} files")
            else:
                logger.info("No existing state file, starting fresh")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            self.state = {}

    def save_state(self):
        """Save state to disk."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)

            data = {
                'files': self.state,
                'last_updated': datetime.now().isoformat()
            }

            # Write to temp file first, then rename (atomic)
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            os.rename(temp_file, self.state_file)

        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def get_position(self, file_path: str) -> Tuple[Optional[int], int]:
        """
        Get saved position for a file.

        Returns:
            Tuple of (inode, position). Returns (None, 0) if file not tracked.
        """
        file_state = self.state.get(file_path, {})
        return file_state.get('inode'), file_state.get('position', 0)

    def save_position(self, file_path: str, inode: int, position: int):
        """Save current position for a file."""
        self.state[file_path] = {
            'inode': inode,
            'position': position,
            'last_modified': datetime.now().isoformat()
        }
        # Save immediately to prevent data loss
        self.save_state()

    def detect_rotation(self, file_path: str, current_inode: int) -> bool:
        """
        Detect if file was rotated.

        Returns:
            True if file was rotated (inode changed), False otherwise.
        """
        saved_inode, _ = self.get_position(file_path)

        if saved_inode is None:
            # First time seeing this file
            return False

        if saved_inode != current_inode:
            logger.info(f"File rotation detected: {file_path} "
                       f"(old inode: {saved_inode}, new inode: {current_inode})")
            return True

        return False


# ============================================================================
# Health Check Server
# ============================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP handler for health check endpoint."""

    shipper_stats = {
        'status': 'starting',
        'uptime_seconds': 0,
        'files_monitored': 0,
        'metrics_shipped': 0,
        'last_ship_time': None,
        'errors_last_hour': 0,
        'datadog_connected': False,
        'start_time': time.time()
    }

    def do_GET(self):
        """Handle GET request."""
        if self.path == '/health':
            # Update uptime
            self.shipper_stats['uptime_seconds'] = int(time.time() - self.shipper_stats['start_time'])

            # Determine status
            if self.shipper_stats['datadog_connected']:
                status_code = 200
                self.shipper_stats['status'] = 'healthy'
            else:
                status_code = 503
                self.shipper_stats['status'] = 'unhealthy'

            # Send response
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(self.shipper_stats, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress request logging."""
        pass


def run_health_check_server(port: int):
    """Run health check HTTP server in background thread."""
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Health check server started on port {port}")
    return server


# ============================================================================
# Main Metrics Shipper
# ============================================================================

class MetricsShipper:
    """
    Main metrics shipping service.

    Monitors metrics files and ships to DataDog with:
    - Duplicate prevention
    - Batch processing
    - Graceful shutdown
    """

    def __init__(self, config: ShipperConfig):
        self.config = config
        self.position_tracker = FilePositionTracker(config.state_file)
        self.shutdown_event = Event()

        # Statistics
        self.total_metrics_shipped = 0
        self.total_errors = 0
        self.last_ship_time = None

        # Initialize DataDog
        self.init_datadog()

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def init_datadog(self):
        """Initialize DataDog API client."""
        try:
            options = {
                'api_key': self.config.datadog_api_key,
                'app_key': self.config.datadog_app_key,
                'api_host': f'https://api.{self.config.datadog_site}'
            }
            initialize(**options)

            # Test connection
            api.Metric.send(metric='armorcode.shipper.startup', points=1)
            logger.info("DataDog connection successful")
            HealthCheckHandler.shipper_stats['datadog_connected'] = True

        except Exception as e:
            logger.error(f"Failed to initialize DataDog: {e}")
            HealthCheckHandler.shipper_stats['datadog_connected'] = False
            raise

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.shutdown_event.set()

    def find_metrics_files(self) -> List[str]:
        """Find all metrics files matching the pattern."""
        pattern = os.path.join(self.config.metrics_dir, self.config.metrics_pattern)
        files = glob.glob(pattern)

        # Filter out rotated files (*.json.YYYY-MM-DD)
        active_files = [f for f in files if not f.split('.')[-1].isdigit()]

        return sorted(active_files)

    def process_file(self, file_path: str) -> int:
        """
        Process a metrics file and return number of metrics read.

        Returns:
            Number of metrics read from file.
        """
        try:
            # Get file inode
            stat_info = os.stat(file_path)
            current_inode = stat_info.st_ino

            # Check for rotation
            saved_inode, saved_position = self.position_tracker.get_position(file_path)

            if self.position_tracker.detect_rotation(file_path, current_inode):
                # File was rotated, start from beginning
                position = 0
                logger.info(f"Starting from beginning due to rotation: {file_path}")
            else:
                # Continue from saved position
                position = saved_position

            # Read metrics from file
            metrics = []
            with open(file_path, 'r') as f:
                f.seek(position)

                for line in f:
                    try:
                        metric = json.loads(line.strip())
                        metrics.append(metric)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Skipping invalid JSON line in {file_path}: {e}")
                        continue

                # Save new position
                new_position = f.tell()
                self.position_tracker.save_position(file_path, current_inode, new_position)

            # Ship metrics if any were read
            if metrics:
                self.ship_metrics_batch(metrics)

            return len(metrics)

        except FileNotFoundError:
            # File was deleted or rotated, remove from state
            logger.info(f"File not found (probably rotated): {file_path}")
            return 0
        except Exception as e:
            logger.error(f"Error processing file {file_path}: {e}")
            self.total_errors += 1
            HealthCheckHandler.shipper_stats['errors_last_hour'] += 1
            return 0

    def convert_to_datadog(self, metric: Dict) -> Dict:
        """
        Convert custom metric format to DataDog format.

        Input format:
        {
            "@timestamp": 1704067200000,
            "metric_name": "http.request.duration_ms",
            "value": 234.56,
            "tags": {"operation": "get_task", "status_code": "200"}
        }

        Output format:
        {
            "metric": "armorcode.http.request.duration_ms",
            "points": [(timestamp_sec, value)],
            "tags": ["operation:get_task", "status_code:200"],
            "type": "gauge"
        }
        """
        timestamp_sec = metric['@timestamp'] / 1000.0
        metric_name = f"armorcode.{metric['metric_name']}"

        tags = [f"{k}:{v}" for k, v in metric.get('tags', {}).items()]

        return {
            'metric': metric_name,
            'points': [(timestamp_sec, metric['value'])],
            'tags': tags,
            'type': 'gauge'
        }

    def ship_metrics_batch(self, metrics: List[Dict]):
        """
        Ship a batch of metrics to DataDog with retry logic.

        Args:
            metrics: List of metrics in custom format
        """
        # Convert to DataDog format
        dd_metrics = [self.convert_to_datadog(m) for m in metrics]

        # Submit with exponential backoff retry
        retry_delays = [1, 2, 4, 8, 16]

        for attempt, delay in enumerate(retry_delays):
            try:
                # DataDog API expects list of metric dictionaries
                api.Metric.send(dd_metrics)

                # Update statistics
                self.total_metrics_shipped += len(metrics)
                self.last_ship_time = datetime.now().isoformat()

                HealthCheckHandler.shipper_stats['metrics_shipped'] = self.total_metrics_shipped
                HealthCheckHandler.shipper_stats['last_ship_time'] = self.last_ship_time

                logger.info(f"Successfully shipped {len(metrics)} metrics to DataDog")
                return

            except Exception as e:
                self.total_errors += 1
                HealthCheckHandler.shipper_stats['errors_last_hour'] += 1

                if attempt == len(retry_delays) - 1:
                    # Max retries reached, log and drop batch
                    logger.error(f"Failed to ship metrics after {attempt+1} attempts: {e}")
                    logger.error(f"Dropping batch of {len(metrics)} metrics")
                else:
                    # Retry with backoff
                    logger.warning(f"Shipping failed (attempt {attempt+1}/{len(retry_delays)}), "
                                 f"retrying in {delay}s: {e}")
                    time.sleep(delay)

    def run(self):
        """Main run loop."""
        logger.info("Metrics shipper started")
        HealthCheckHandler.shipper_stats['status'] = 'running'

        poll_interval = self.config.batch_timeout_sec

        while not self.shutdown_event.is_set():
            try:
                # Find all metrics files
                files = self.find_metrics_files()
                HealthCheckHandler.shipper_stats['files_monitored'] = len(files)

                if not files:
                    logger.debug(f"No metrics files found matching pattern: {self.config.metrics_pattern}")

                # Process each file
                total_read = 0
                for file_path in files:
                    count = self.process_file(file_path)
                    total_read += count

                if total_read > 0:
                    logger.info(f"Processed {total_read} metrics from {len(files)} files")

                # Sleep until next poll
                self.shutdown_event.wait(timeout=poll_interval)

            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
                self.shutdown_event.wait(timeout=10)  # Backoff on error

        logger.info("Metrics shipper stopped")

    def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down metrics shipper...")

        # Process any remaining metrics
        files = self.find_metrics_files()
        for file_path in files:
            self.process_file(file_path)

        # Save final state
        self.position_tracker.save_state()

        logger.info(f"Shutdown complete. Total metrics shipped: {self.total_metrics_shipped}, "
                   f"Total errors: {self.total_errors}")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point."""
    try:
        # Load configuration
        config = ShipperConfig()

        # Start health check server
        health_server = run_health_check_server(config.health_check_port)

        # Create and start shipper
        shipper = MetricsShipper(config)

        try:
            shipper.run()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        finally:
            shipper.shutdown()

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
