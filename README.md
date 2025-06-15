# ArgoCD-local

For personal projects, I don't always need ArgoCD running in my k8s cluster taking up resources. So I created this script so that I can spin up ArgoCD locally when I need it.

## Features
- Creates a local Kubernetes cluster with k3d
- Installs ArgoCD via Helm
- Configures ArgoCD ingress for local access
- Sets up GitHub repository credentials for ArgoCD
- Optionally configures Azure AKS cluster credentials for ArgoCD
- Caches selections and credentials for convenience

## Requirements
- [k3d](https://k3d.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [helm](https://helm.sh/)
- [gh (GitHub CLI)](https://cli.github.com/)
- [az (Azure CLI)](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)

## Usage

```sh
python3 argocd-local.py
```

You may be prompted to select Azure subscriptions, AKS clusters, or provide your GitHub repository. The script will guide you through the setup process.

### Example

1. Run the script:
   ```sh
   python3 argocd-local.py start
   ```
2. Follow the prompts to select your Azure subscription and AKS cluster (if needed).
3. Enter your GitHub repository in the format `owner/repo_name` when prompted.
4. Access the ArgoCD UI at [http://localhost:8080/](http://localhost:8080/) with the credentials provided by the script.

## Notes
- The script caches your selections and credentials in `argocd_local_config.json` for convenience.
- Make sure you are authenticated with both the GitHub CLI (`gh auth login`) and Azure CLI (`az login`) before running the script.
