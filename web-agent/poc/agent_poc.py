#!/usr/bin/env python3
"""
Agent POC - 3-Module Queue-Based Architecture

Module 1: Task Fetcher (1 thread)
  - Polls server for tasks
  - Puts tasks into task_queue

Module 2: Task Processor (N threads)
  - Gets tasks from task_queue
  - Prints task name every 1 second for N iterations
  - Puts completed tasks into response_queue

Module 3: Result Uploader (1 thread)
  - Gets completed tasks from response_queue
  - Posts completion to server
"""

import requests
import time
import threading
from queue import Queue, Empty
import argparse
import sys


class AgentPOC:
    def __init__(self, server_url, pool_size=3, response_queue_threshold=80):
        self.server_url = server_url
        self.pool_size = pool_size
        self.response_queue_threshold = response_queue_threshold

        # Queues
        self.task_queue = Queue(maxsize=100)
        self.response_queue = Queue(maxsize=100)

        # Statistics
        self.stats = {
            'tasks_fetched': 0,
            'tasks_processed': 0,
            'tasks_completed': 0
        }
        self.stats_lock = threading.Lock()

        # Running flag
        self.running = True

    def update_stats(self, key):
        """Thread-safe statistics update"""
        with self.stats_lock:
            self.stats[key] += 1

    def print_stats(self):
        """Print current statistics"""
        with self.stats_lock:
            print(f"\n{'='*60}")
            print(f"Agent Statistics:")
            print(f"  Tasks Fetched:   {self.stats['tasks_fetched']}")
            print(f"  Tasks Processed: {self.stats['tasks_processed']}")
            print(f"  Tasks Completed: {self.stats['tasks_completed']}")
            print(f"  Task Queue:      {self.task_queue.qsize()}/100")
            print(f"  Response Queue:  {self.response_queue.qsize()}/100")
            print(f"{'='*60}\n")

    # ============================================================
    # Module 1: Task Fetcher
    # ============================================================
    def task_fetcher_worker(self):
        """
        Module 1: Fetch tasks from server
        - Implements backpressure (checks response queue size)
        - Uses AC semaphore for concurrency control
        """
        print("[MODULE 1] Task Fetcher started")

        while self.running:
            try:
                # BACKPRESSURE: Check if response queue is near full
                if self.task_queue.qsize() >= self.response_queue_threshold:
                    print(f"[MODULE 1] Response queue near full "
                          f"({self.task_queue.qsize()}/{self.task_queue.maxsize}), "
                          f"pausing fetching for 2s")
                    time.sleep(2)
                    continue

                try:
                    # Fetch task from server
                    print("[MODULE 1] Fetching task from server...")
                    response = requests.get(
                        f"{self.server_url}/get-task",
                        timeout=10
                    )

                    if response.status_code == 200:
                        data = response.json()
                        task = data.get('data')

                        if task:
                            print(f"[MODULE 1] Fetched task: {task['taskId']} - "
                                  f"{task['taskName']} ({task['iterations']} iterations)")

                            # Put task in queue
                            self.task_queue.put(task, timeout=5)
                            self.update_stats('tasks_fetched')

                            print(f"[MODULE 1] Task queued. Queue size: {self.task_queue.qsize()}")
                        else:
                            print("[MODULE 1] No task data received")
                            time.sleep(1)
                    else:
                        print(f"[MODULE 1] Server error: {response.status_code}")
                        time.sleep(2)

                finally:
                    print("[MODULE 1] loop complete")

                # Small delay between fetches
                time.sleep(0.5)

            except requests.exceptions.RequestException as e:
                print(f"[MODULE 1] Network error: {e}")
                time.sleep(5)
            except Exception as e:
                print(f"[MODULE 1] Error: {e}")
                time.sleep(5)

        print("[MODULE 1] Task Fetcher stopped")

    # ============================================================
    # Module 2: Task Processor Pool
    # ============================================================
    def task_processor_worker(self, worker_id):
        """
        Module 2: Process tasks
        - Gets task from task_queue
        - Prints task name every 1 second for N iterations
        - Puts completed task into response_queue
        """
        print(f"[MODULE 2-{worker_id}] Task Processor started")

        while self.running:
            try:
                # Block until task is available (with timeout for clean shutdown)
                task = self.task_queue.get(timeout=1)

                task_id = task['taskId']
                task_name = task['taskName']
                iterations = task['iterations']

                print(f"[MODULE 2-{worker_id}] Processing task: {task_id} - {task_name}")

                # Simulate work: Print task name every 1 second for N iterations
                for i in range(iterations):
                    print(f"[MODULE 2-{worker_id}] Task {task_name}: iteration {i+1}/{iterations}")
                    time.sleep(1)  # 1 second delay

                print(f"[MODULE 2-{worker_id}] Completed task: {task_id} - {task_name}")

                # Put completed task into response queue
                self.response_queue.put(task, timeout=5)
                self.update_stats('tasks_processed')

                print(f"[MODULE 2-{worker_id}] Task queued for upload. "
                      f"Response queue size: {self.response_queue.qsize()}")

            except Empty:
                # Queue timeout, continue loop
                continue
            except Exception as e:
                print(f"[MODULE 2-{worker_id}] Error: {e}")
                time.sleep(1)

        print(f"[MODULE 2-{worker_id}] Task Processor stopped")

    # ============================================================
    # Module 3: Result Uploader
    # ============================================================
    def result_uploader_worker(self):
        """
        Module 3: Upload results to server
        - Gets completed tasks from response_queue
        - Posts completion to server
        - Uses AC semaphore for concurrency control
        """
        print("[MODULE 3] Result Uploader started")

        while self.running:
            try:
                # Block until result is available (with timeout for clean shutdown)
                task = self.response_queue.get(timeout=1)

                task_id = task['taskId']
                task_name = task['taskName']

                print(f"[MODULE 3] Uploading result for task: {task_id} - {task_name}")

                try:
                    # Post completion to server
                    response = requests.post(
                        f"{self.server_url}/complete-task",
                        json={'taskId': task_id},
                        timeout=10
                    )

                    if response.status_code == 200:
                        print(f"[MODULE 3] Successfully completed task: {task_id} - {task_name}")
                        self.update_stats('tasks_completed')
                    else:
                        print(f"[MODULE 3] Server error: {response.status_code}")

                finally:
                    print("[MODULE 3] loop complete")

            except Empty:
                # Queue timeout, continue loop
                continue
            except requests.exceptions.RequestException as e:
                print(f"[MODULE 3] Network error: {e}")
                time.sleep(5)
            except Exception as e:
                print(f"[MODULE 3] Error: {e}")
                time.sleep(1)

        print("[MODULE 3] Result Uploader stopped")

    # ============================================================
    # Main Agent
    # ============================================================
    def start(self):
        """Start all modules"""
        print("="*60)
        print("Starting Agent POC - 3-Module Architecture")
        print("="*60)
        print(f"Server URL: {self.server_url}")
        print(f"Processor Pool Size: {self.pool_size}")
        print(f"Response Queue Threshold: {self.response_queue_threshold}")
        print(f"AC Server Max Concurrent: 2")
        print("="*60)

        threads = []

        # Start Module 1: Task Fetcher (1 thread)
        t1 = threading.Thread(target=self.task_fetcher_worker, name="TaskFetcher")
        t1.start()
        threads.append(t1)

        # Start Module 2: Task Processor Pool (N threads)
        for i in range(self.pool_size):
            t2 = threading.Thread(
                target=self.task_processor_worker,
                args=(i+1,),
                name=f"TaskProcessor-{i+1}"
            )
            t2.start()
            threads.append(t2)

        # Start Module 3: Result Uploader (1 thread)
        t3 = threading.Thread(target=self.result_uploader_worker, name="ResultUploader")
        t3.start()
        threads.append(t3)

        print(f"\nStarted {len(threads)} threads:")
        print(f"  - 1 Task Fetcher")
        print(f"  - {self.pool_size} Task Processors")
        print(f"  - 1 Result Uploader")
        print("\nAgent running... Press Ctrl+C to stop\n")

        # Print stats every 10 seconds
        try:
            while True:
                time.sleep(10)
                self.print_stats()
        except KeyboardInterrupt:
            print("\n\nShutting down agent...")
            self.running = False

            # Wait for all threads to finish
            for t in threads:
                t.join(timeout=5)

            self.print_stats()
            print("Agent stopped.")


def main():
    parser = argparse.ArgumentParser(description="Agent POC - Queue-Based Architecture")
    parser.add_argument(
        '--server',
        type=str,
        default='http://localhost:5123',
        help='Mock server URL (default: http://localhost:5123)'
    )
    parser.add_argument(
        '--pool-size',
        type=int,
        default=3,
        help='Number of task processor threads (default: 3)'
    )
    parser.add_argument(
        '--threshold',
        type=int,
        default=2,
        help='Response queue threshold for backpressure (default: 80)'
    )

    args = parser.parse_args()

    # Create and start agent
    agent = AgentPOC(
        server_url=args.server,
        pool_size=args.pool_size,
        response_queue_threshold=args.threshold
    )

    agent.start()


if __name__ == '__main__':
    main()
