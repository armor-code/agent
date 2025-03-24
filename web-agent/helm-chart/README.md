# ArmorCode Web Agent Helm Chart

This Helm chart deploys the ArmorCode Web Agent on Kubernetes. The chart supports two deployment patterns:

1. **Single Deployment** - One deployment with multiple replicas, all using the same API key
2. **Multiple Deployments** - Multiple separate deployments, each with its own API key

## Prerequisites

- Kubernetes 1.16+
- Helm 3.0+

## Installation

### Single Deployment

For a single deployment with one API key:

```bash
# Create a values file (my-values.yaml)
cat <<EOF > my-values.yaml
singleDeployment:
  enabled: true
  replicaCount: 1
  apiKey: your-api-key

agentDefaults:
  serverUrl: https://app.armorcode.com
  
# The image will be pulled from the registry
image:
  repository: docker.io/armorcode/armorcode-web-agent
  tag: latest
  pullPolicy: IfNotPresent
EOF

# Install the chart
helm install armorcode-web-agent ./helm-chart -f my-values.yaml
```

### Multiple Deployments

For multiple deployments with different API keys:

```bash
# Install the chart using the provided multi-agent-values.yaml
helm install armorcode-web-agents ./helm-chart -f multi-agent-values.yaml
```

The `multi-agent-values.yaml` is configured to pull the ArmorCode Web Agent image from Docker Hub:

```yaml
image:
  repository: docker.io/armorcode/armorcode-web-agent
  tag: latest
  pullPolicy: IfNotPresent
```

You can modify these values to use your preferred container registry or image version.

## Configuration

### Common Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Image repository | `armorcode/armorcode-web-agent` |
| `image.tag` | Image tag | `latest` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `persistence.enabled` | Enable persistence | `true` |
| `persistence.size` | PVC size | `1Gi` |
| `persistence.accessMode` | PVC access mode | `ReadWriteOnce` |
| `networkPolicy.enabled` | Enable network policy | `true` |

### Agent Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `agentDefaults.serverUrl` | ArmorCode server URL | `https://app.armorcode.com` |
| `agentDefaults.debugMode` | Enable debug mode | `false` |
| `agentDefaults.envName` | Environment name | `""` |
| `agentDefaults.index` | Agent index | `_prod` |
| `agentDefaults.timeout` | Request timeout (seconds) | `30` |
| `agentDefaults.verify` | Verify SSL certificates | `false` |
| `agentDefaults.poolSize` | Thread pool size | `5` |
| `agentDefaults.uploadToAc` | Upload to ArmorCode | `true` |

### Single Deployment

| Parameter | Description | Default |
|-----------|-------------|---------|
| `singleDeployment.enabled` | Enable single deployment | `true` |
| `singleDeployment.replicaCount` | Number of replicas | `1` |
| `singleDeployment.apiKey` | API key | `""` |

### Multiple Deployments

| Parameter | Description | Default |
|-----------|-------------|---------|
| `multipleDeployments.enabled` | Enable multiple deployments | `false` |
| `multipleDeployments.instances` | List of instances with name and API key | `[]` |

Example of instances configuration:

```yaml
multipleDeployments:
  enabled: true
  instances:
    - name: prod
      apiKey: api-key-1
      envName: production
    - name: staging
      apiKey: api-key-2
      envName: staging
```

## Uninstallation

```bash
helm uninstall armorcode-web-agent
```

## Persistence and Logging

This chart uses a ReadWriteMany (RWX) persistent volume to centralize logs from all agent pods, even when they run on different nodes. Each agent writes to its own subdirectory within the volume, using its instance name (e.g., "prod", "staging", "dev").

### Storage Classes

You'll need to configure an appropriate ReadWriteMany storage class based on your Kubernetes cluster environment:

```yaml
persistence:
  enabled: true
  accessMode: ReadWriteMany
  storageClassName: "storage-class-name"
  size: 5Gi
```

Recommended storage classes by platform:
- AWS: "efs"
- GCP: "filestore" 
- Azure: "azurefile"
- On-premises: "nfs"

### Accessing Logs

Logs are stored in `/tmp/armorcode/log` within each agent's subdirectory on the persistent volume. You can access them through:

1. Using `kubectl exec` to connect to any pod and view logs across all agents
```bash
kubectl exec -it <any-pod-name> -- ls -la /tmp/armorcode/*/log
```

2. Mounting the PVC to a dedicated logging pod
```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: log-viewer
spec:
  containers:
  - name: log-viewer
    image: alpine
    command: ["sh", "-c", "tail -f /logs/*/log/*.log"]
    volumeMounts:
    - name: armorcode-data
      mountPath: /logs
  volumes:
  - name: armorcode-data
    persistentVolumeClaim:
      claimName: armorcode-web-agent
EOF
```

## Support

For support, contact ArmorCode at support@armorcode.com
