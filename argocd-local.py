#!/bin/python3
"""
ArgoCD Local - A script to run ArgoCD locally in a k3d cluster

This script creates a local k3d Kubernetes cluster, installs ArgoCD,
and configures it for use with GitHub repositories and optional AKS clusters.
"""

import os
import subprocess
import yaml
import base64
import argparse
from pathlib import Path
import sys
import json
import textwrap
from enum import Enum
from dataclasses import dataclass
from typing import NamedTuple


class Colors(Enum):
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class ArgocdLocalError(Exception):
    """Base exception for argocd-local script errors."""

    pass


class AksSelection(NamedTuple):
    """Result of AKS cluster selection."""

    subscription_id: str
    resource_group: str
    cluster_name: str


@dataclass
class AksClusterConfig:
    """Configuration for an AKS cluster."""

    subscription_id: str
    resource_group: str
    cluster_name: str


CONFIG_FILE = Path(__file__).resolve().parent / "argocd_local_config.json"


def load_config(config_path: Path) -> dict:
    """Load configuration from a JSON file."""
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: Config file {config_path} is corrupted. Starting fresh.")
            return {}
    return {}


def save_config(config_path: Path, data: dict) -> None:
    """Save configuration to a JSON file."""
    with open(config_path, "w") as f:
        json.dump(data, f, indent=4)


def run_command(
    command, check=True, shell=False, cwd=None, input=None, print_output=False
) -> subprocess.CompletedProcess:
    """Run a shell command and return the output."""
    print(f"Running: {' '.join(command) if isinstance(command, list) else command}")

    if shell:
        result = subprocess.run(
            command,
            shell=True,
            check=check,
            text=True,
            capture_output=True,
            cwd=cwd,
            input=input,
        )
    else:
        result = subprocess.run(
            command, check=check, text=True, capture_output=True, cwd=cwd, input=input
        )

    if result.stdout and print_output:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result


def list_and_select_aks_clusters() -> AksSelection:
    """List all AKS clusters across all subscriptions and let the user select one based on the access they have"""
    config = load_config(CONFIG_FILE)
    cached_selection = config.get("aks_selection")

    if (
        cached_selection
        and cached_selection.get("subscription_id")
        and cached_selection.get("resource_group")
        and cached_selection.get("cluster_name")
    ):
        print("Using cached AKS cluster selection:")
        print(f"  Subscription ID: {cached_selection['subscription_id']}")
        print(f"  Resource Group: {cached_selection['resource_group']}")
        print(f"  Cluster Name: {cached_selection['cluster_name']}")
        use_cached = input("Use this cached selection? (y/n): ").lower()
        if use_cached == "y":
            print("\nSetting subscription to ensure context is correct.")
            run_command(
                [
                    "az",
                    "account",
                    "set",
                    "--subscription",
                    cached_selection["subscription_id"],
                ]
            )
            return AksSelection(
                cached_selection["subscription_id"],
                cached_selection["resource_group"],
                cached_selection["cluster_name"],
            )

    try:
        print("Fetching all azure subscriptions")
        sub_result = run_command(
            [
                "az",
                "account",
                "list",
                "--query",
                "[].{Name:name,SubscriptionId:id}",
                "-o",
                "json",
            ]
        )
        subscriptions = json.loads(sub_result.stdout)

        if not subscriptions:
            print("No subscriptions found. Please log in with 'az login'.")
            return AksSelection("", "", "")

        print("\nAvailable azure subscriptions:")
        for i, sub in enumerate(subscriptions):
            print(f"{i + 1}. {sub['Name']} ({sub['SubscriptionId']})")

        while True:
            try:
                sub_choice = int(input("\nSelect a subscription (number): ")) - 1
                if 0 <= sub_choice < len(subscriptions):
                    selected_subscription = subscriptions[sub_choice]
                    break
                print("Invalid selection. Please try again.")
            except ValueError:
                print("Please enter a valid number.")

        subscription_id = selected_subscription["SubscriptionId"]
        print(f"\nSetting subscription to: {selected_subscription['Name']}")
        run_command(["az", "account", "set", "--subscription", subscription_id])

        print("\nFetching available AKS clusters")
        clusters_result = run_command(
            [
                "az",
                "aks",
                "list",
                "--query",
                "[].{Name:name,ResourceGroup:resourceGroup,Location:location}",
                "-o",
                "json",
            ]
        )
        clusters = json.loads(clusters_result.stdout)

        if not clusters:
            print(
                f"No AKS clusters found in subscription {selected_subscription['Name']}"
            )
            return AksSelection("", "", "")

        print("\nAvailable AKS clusters:")
        for i, cluster in enumerate(clusters):
            print(
                f"{i + 1}, {cluster['Name']} (Resource Group: {cluster['ResourceGroup']}, Location: {cluster['Location']})"
            )

        while True:
            try:
                cluster_choice = int(input("\nSelect a cluster (number): ")) - 1
                if 0 <= cluster_choice < len(clusters):
                    selected_cluster = clusters[cluster_choice]
                    break
                print("Invalid selection. Please try again")
            except ValueError:
                print("Please enter a valid number")

        # Save the selection
        config["aks_selection"] = {
            "subscription_id": subscription_id,
            "resource_group": selected_cluster["ResourceGroup"],
            "cluster_name": selected_cluster["Name"],
        }
        save_config(CONFIG_FILE, config)

        return AksSelection(
            subscription_id,
            selected_cluster["ResourceGroup"],
            selected_cluster["Name"],
        )

    except Exception as e:
        print(f"Error listing Azure resources: {str(e)}")
        return AksSelection("", "", "")


def check_dependencies():
    """Check if required dependencies are installed."""
    dependencies = ["k3d", "helm", "kubectl", "git", "gh", "az"]

    for dep in dependencies:
        try:
            run_command(["which", dep])
        except subprocess.CalledProcessError:
            print(
                f"Error: {dep} is not installed. Please install it before proceeding."
            )
            sys.exit(1)

    print("All dependencies are installed.")


def create_k3d_cluster(cluster_name="gitops-local") -> bool:
    """Create a k3d cluster if it doesn't exist."""
    try:
        result = run_command(["k3d", "cluster", "list", "-o", "json"], check=False)
        clusters = []
        if result.stdout:
            clusters = json.loads(result.stdout)

        # More pythonic way to check if cluster exists
        cluster_exists = any(
            cluster.get("name") == cluster_name for cluster in clusters
        )

        if cluster_exists:
            print(f"Cluster {cluster_name} already exists, using it.")
        else:
            print(f"Creating k3d cluster: {cluster_name}")
            run_command(
                [
                    "k3d",
                    "cluster",
                    "create",
                    cluster_name,
                    "--port",
                    "8080:80@loadbalancer",
                    "--port",
                    "8443:443@loadbalancer",
                ]
            )

        run_command(["kubectl", "config", "use-context", "k3d-" + cluster_name])
        return True
    except Exception as e:
        print(f"Error creating k3d cluster: {str(e)}")
        return False


def configure_argocd_ingress(namespace="argocd"):
    """Create a custom Ingress for ArgoCD that works with localhost."""
    try:
        ingress_yaml = f"""
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: argocd-server
  namespace: {namespace}
  annotations:
    kubernetes.io/ingress.class: traefik
spec:
  rules:
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: argocd-server
            port:
              number: 80
"""
        run_command(["kubectl", "apply", "-f", "-"], input=ingress_yaml)
        print("Successfully applied custom ArgoCD Ingress.")
        return True
    except Exception as e:
        print(f"Error configuring ArgoCD Ingress: {str(e)}")
        return False


def install_argocd(namespace="argocd", cluster_name="gitops-local"):
    """Install ArgoCD using Helm."""
    try:
        output = run_command(["kubectl", "config", "current-context"])
        if output.stdout != "":
            if output.stdout.strip() != f"k3d-{cluster_name}":
                print("incorrect k8s context, switching to k3d cluster")
                return

        run_command(["kubectl", "create", "namespace", namespace], check=False)

        run_command(
            ["helm", "repo", "add", "argo", "https://argoproj.github.io/argo-helm"]
        )
        run_command(["helm", "repo", "update"])

        run_command(
            [
                "helm",
                "upgrade",
                "--install",
                "argocd",
                "argo/argo-cd",
                "--namespace",
                namespace,
                "--set",
                "server.extraArgs={--insecure}",
                "--set",
                "server.insecure=true",
                "--set",
                "server.service.type=ClusterIP",
                "--set",
                "server.ingress.enabled=false",
            ]
        )

        print("Waiting for ArgoCD pods to be ready...")
        run_command(
            [
                "kubectl",
                "wait",
                "--namespace",
                namespace,
                "--for=condition=ready",
                "pod",
                "--selector=app.kubernetes.io/name=argocd-server",
                "--timeout=300s",
            ]
        )

        return True
    except Exception as e:
        print(f"Error installing ArgoCD: {str(e)}")
        return False


def setup_github_credentials(namespace="argocd", github_repo_specifier=None):
    """Configure GitHub repository credentials in ArgoCD using gh CLI."""
    actual_repo_url = None
    config = load_config(CONFIG_FILE)
    cached_repo = config.get("github_repo")

    if not github_repo_specifier:
        if cached_repo:
            print(f"Using cached GitHub repository: {cached_repo}")
            use_cached = input("Use this cached repository? (y/n): ").lower()
            if use_cached == "y":
                github_repo_specifier = cached_repo
            else:
                github_repo_specifier = input(
                    "Enter your GitHub repository (e.g., owner/repo_name): "
                )
        else:
            github_repo_specifier = input(
                "Enter your GitHub repository (e.g., owner/repo_name): "
            )

    if not github_repo_specifier or "/" not in github_repo_specifier:
        print(
            f"Invalid GitHub repository format: '{github_repo_specifier}'. Expected 'owner/repo_name'."
        )
        return False

    actual_repo_url = f"https://github.com/{github_repo_specifier}.git"
    print(f"Using GitHub repository URL: {actual_repo_url}")

    secret_name = "github-repo-creds"
    try:
        check_secret_command = [
            "kubectl",
            "get",
            "secret",
            secret_name,
            "-n",
            namespace,
        ]
        run_command(check_secret_command, check=True)
        print(
            f"Secret {secret_name} already exists in namespace {namespace}. Skipping GitHub credential setup."
        )
        return True
    except subprocess.CalledProcessError:
        print(f"Secret {secret_name} not found. Proceeding with setup...")
    except Exception as e:
        print(
            f"Error checking for existing secret {secret_name}: {str(e)}. Proceeding with setup..."
        )

    github_token = None
    try:
        auth_status_result = run_command(["gh", "auth", "status"], check=False)
        if auth_status_result.returncode != 0:
            print(
                "GitHub CLI not authenticated. Please run 'gh auth login' and try again."
            )
            return False

        token_result = run_command(["gh", "auth", "token"], check=False)
        if token_result.returncode == 0 and token_result.stdout.strip():
            github_token = token_result.stdout.strip()
            print("Successfully retrieved GitHub token using gh CLI.")
        else:
            print(
                "Failed to retrieve GitHub token using gh CLI. Ensure you are logged in."
            )
            return False

    except Exception as e:
        print(f"Error interacting with gh CLI: {str(e)}")
        return False

    if not github_token:
        return False

    if not cached_repo or cached_repo != github_repo_specifier:
        config["github_repo"] = github_repo_specifier
        save_config(CONFIG_FILE, config)

    try:
        secret_yaml = f"""
apiVersion: v1
kind: Secret
metadata:
  name: github-repo-creds
  namespace: {namespace}
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: {actual_repo_url}
  password: {github_token}
  username: x-access-token
"""

        run_command(["kubectl", "apply", "-f", "-"], input=secret_yaml)

        print("Successfully applied GitHub repository credentials secret.")
        return True
    except Exception as e:
        print(f"Error setting up GitHub credentials: {str(e)}")
        return False


def setup_aks_credentials(
    namespace="argocd",
    subscription_id=None,
    resource_group=None,
    aks_cluster_name=None,
    from_args=False,
) -> bool:
    """Configure AKS cluster credentials in ArgoCD using Azure CLI."""
    config = load_config(CONFIG_FILE)

    try:
        run_command(["az", "account", "show"], check=True)
        print("Successfully authenticated with Azure CLI.")
    except subprocess.CalledProcessError:
        print("Azure CLI not authenticated. Please run 'az login' and try again.")
        return False
    except Exception as e:
        print(f"Error checking Azure CLI authentication: {str(e)}")
        return False

    if not (subscription_id and resource_group and aks_cluster_name):
        cached_selection = config.get("aks_selection")
        if (
            cached_selection
            and cached_selection.get("subscription_id")
            and cached_selection.get("resource_group")
            and cached_selection.get("cluster_name")
        ):
            print("Using cached AKS cluster selection for credential setup:")
            print(f"  Subscription ID: {cached_selection['subscription_id']}")
            print(f"  Resource Group: {cached_selection['resource_group']}")
            print(f"  Cluster Name: {cached_selection['cluster_name']}")
            use_cached = input(
                "Use this cached selection for AKS credentials? (y/n): "
            ).lower()
            if use_cached == "y":
                subscription_id = cached_selection["subscription_id"]
                resource_group = cached_selection["resource_group"]
                aks_cluster_name = cached_selection["cluster_name"]
                print(f"\nSetting subscription to: {subscription_id} (from cache)")
                run_command(["az", "account", "set", "--subscription", subscription_id])
            else:
                print("Proceeding with manual AKS cluster selection for credentials.")
                (
                    selected_subscription_id,
                    selected_resource_group,
                    selected_aks_cluster_name,
                ) = list_and_select_aks_clusters()
                if not (
                    selected_subscription_id
                    and selected_resource_group
                    and selected_aks_cluster_name
                ):
                    print("Failed to select a cluster for credential setup.")
                    return False
                subscription_id = selected_subscription_id
                resource_group = selected_resource_group
                aks_cluster_name = selected_aks_cluster_name
        else:
            print("Starting AKS cluster selection for credential setup.")
            (
                selected_subscription_id,
                selected_resource_group,
                selected_aks_cluster_name,
            ) = list_and_select_aks_clusters()
            if not (
                selected_subscription_id
                and selected_resource_group
                and selected_aks_cluster_name
            ):
                print("Failed to select a cluster for credential setup.")
                return False
            subscription_id = selected_subscription_id
            resource_group = selected_resource_group
            aks_cluster_name = selected_aks_cluster_name
    elif from_args:
        config["aks_selection"] = {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": aks_cluster_name,
        }
        save_config(CONFIG_FILE, config)
        print("Saved provided AKS parameters to config.")
        print(f"\nSetting subscription to: {subscription_id} (from arguments)")
        run_command(["az", "account", "set", "--subscription", subscription_id])

    secret_name = f"aks-cluster-secret-{aks_cluster_name.lower().replace('_', '-')}"
    try:
        check_secret_command = [
            "kubectl",
            "get",
            "secret",
            secret_name,
            "-n",
            namespace,
        ]
        run_command(check_secret_command, check=True)
        print(
            f"Secret {secret_name} already exists in namespace {namespace}. Skipping AKS credential setup."
        )
        return True
    except subprocess.CalledProcessError:
        print(f"Secret {secret_name} not found. Proceeding with setup...")
    except Exception as e:
        print(
            f"Error checking for existing secret {secret_name}: {str(e)}. Proceeding with setup..."
        )

    try:
        run_command(["az", "account", "set", "--subscription", subscription_id])

        temp_kubeconfig_dir = Path.home() / ".kube" / "argocd_temp_configs"
        temp_kubeconfig_dir.mkdir(parents=True, exist_ok=True)
        temp_kubeconfig_path = temp_kubeconfig_dir / f"{aks_cluster_name}_config"

        run_command(
            [
                "az",
                "aks",
                "get-credentials",
                "--resource-group",
                resource_group,
                "--name",
                aks_cluster_name,
                "--file",
                str(temp_kubeconfig_path),
                "--overwrite-existing",
            ]
        )
        print(
            f"Successfully retrieved AKS credentials for {aks_cluster_name} into {temp_kubeconfig_path}"
        )

        with open(temp_kubeconfig_path, "r") as f:
            kubeconfig = yaml.safe_load(f)

        current_context_name = kubeconfig.get("current-context")
        if not current_context_name:
            print(
                "Error: Could not determine current-context from the fetched kubeconfig."
            )
            return False

        selected_context = next(
            (
                c
                for c in kubeconfig.get("contexts", [])
                if c.get("name") == current_context_name
            ),
            None,
        )
        if not selected_context:
            print(
                f"Error: Context '{current_context_name}' not found in fetched kubeconfig."
            )
            return False

        cluster_ref = selected_context.get("context", {}).get("cluster")
        cluster_info = next(
            (c for c in kubeconfig.get("clusters", []) if c.get("name") == cluster_ref),
            None,
        )
        if not cluster_info:
            print(f"Error: Cluster '{cluster_ref}' not found.")
            return False

        user_ref = selected_context.get("context", {}).get("user")
        user_info = next(
            (u for u in kubeconfig.get("users", []) if u.get("name") == user_ref), None
        )
        if not user_info:
            print(f"Error: User '{user_ref}' not found.")
            return False

        filtered_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": current_context_name,
            "contexts": [selected_context],
            "clusters": [cluster_info],
            "users": [user_info],
        }

        secret_yaml = f"""
apiVersion: v1
kind: Secret
metadata:
  name: aks-cluster-secret-{aks_cluster_name.lower().replace("_", "-")}
  namespace: {namespace}
  labels:
    argocd.argoproj.io/secret-type: cluster
stringData:
  name: {current_context_name}
  server: {cluster_info.get("cluster", {}).get("server", "")}
  config: |
{textwrap.indent(json.dumps(filtered_kubeconfig, indent=4), "    ")}
"""

        run_command(["kubectl", "apply", "-f", "-"], input=secret_yaml)
        print(f"Successfully created AKS cluster secret for {aks_cluster_name}.")

        try:
            os.remove(temp_kubeconfig_path)
            print(f"Cleaned up temporary kubeconfig: {temp_kubeconfig_path}")
        except OSError as e:
            print(
                f"Error deleting temporary kubeconfig {temp_kubeconfig_path}: {e.strerror}"
            )

        return True

    except subprocess.CalledProcessError as e:
        print(f"Azure CLI command failed: {e}")
        return False
    except FileNotFoundError:
        print(
            f"Error: Temporary kubeconfig file {temp_kubeconfig_path} not found after 'az aks get-credentials'."
        )
        return False
    except Exception as e:
        print(f"Error setting up AKS credentials: {str(e)}")
        return False


def get_argocd_password(namespace="argocd"):
    """Get the initial admin password for ArgoCD."""
    try:
        result = run_command(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "secret",
                "argocd-initial-admin-secret",
                "-o",
                'jsonpath="{.data.password}"',
            ]
        )
        password = base64.b64decode(result.stdout.strip('"').encode()).decode()

        print(
            f"\n{Colors.BOLD.value}{Colors.CYAN.value}ArgoCD UI:{Colors.RESET.value} {Colors.CYAN.value}http://localhost:8080/{Colors.RESET.value}"
        )
        print(f"\n{Colors.BOLD.value}ArgoCD Username:{Colors.RESET.value} admin")
        print(
            f"{Colors.BOLD.value}{Colors.GREEN.value}ArgoCD Password:{Colors.RESET.value} {Colors.YELLOW.value}{password}{Colors.RESET.value}"
        )

        return password
    except Exception as e:
        print(f"Error getting ArgoCD password: {str(e)}")
        return None


def start(args):
    """Starts the local ArgoCD environment."""
    print("Starting local ArgoCD environment...")
    config = load_config(CONFIG_FILE)

    check_dependencies()

    if not create_k3d_cluster():
        return

    if not install_argocd(args.namespace):
        print("failed to install argocd")
        return

    if not configure_argocd_ingress(args.namespace):
        print("Warning: failed to configure ingress for argocd")

    github_repo_to_use = args.github_repo
    if not github_repo_to_use and config.get("github_repo"):
        print(f"Found cached GitHub repository: {config.get('github_repo')}")
        if input("Use this cached repository? (y/n): ").lower() == "y":
            github_repo_to_use = config.get("github_repo")

    if github_repo_to_use:
        if not setup_github_credentials(args.namespace, github_repo_to_use):
            print("Failed to setup GitHub credentials. Continuing without it.")
    elif args.github_repo:
        if not setup_github_credentials(args.namespace, args.github_repo):
            print(
                "Failed to setup GitHub credentials using provided arg. Continuing without it."
            )
    else:
        if not setup_github_credentials(args.namespace, None):
            print("Failed to setup GitHub credentials. Continuing without it.")

    aks_subscription_id_to_use = args.aks_subscription_id
    aks_resource_group_to_use = args.aks_resource_group
    aks_cluster_name_to_use = args.aks_cluster_name
    from_args_flag = False

    if (
        aks_subscription_id_to_use
        and aks_resource_group_to_use
        and aks_cluster_name_to_use
    ):
        from_args_flag = True
    elif args.setup_aks:
        cached_aks_selection = config.get("aks_selection")
        if cached_aks_selection:
            print("Found cached AKS selection:")
            print(f"  Subscription: {cached_aks_selection.get('subscription_id')}")
            print(f"  Resource Group: {cached_aks_selection.get('resource_group')}")
            print(f"  Cluster Name: {cached_aks_selection.get('cluster_name')}")
            if input("Use this cached AKS selection? (y/n): ").lower() == "y":
                aks_subscription_id_to_use = cached_aks_selection.get("subscription_id")
                aks_resource_group_to_use = cached_aks_selection.get("resource_group")
                aks_cluster_name_to_use = cached_aks_selection.get("cluster_name")

    if args.setup_aks or (
        aks_subscription_id_to_use
        and aks_resource_group_to_use
        and aks_cluster_name_to_use
    ):
        if not setup_aks_credentials(
            args.namespace,
            aks_subscription_id_to_use,
            aks_resource_group_to_use,
            aks_cluster_name_to_use,
            from_args=from_args_flag,
        ):
            print("Failed to setup AKS credentials.")

    get_argocd_password(args.namespace)
    print("Local ArgoCD environment started.")


def stop(args):
    """Stops the local ArgoCD environment."""
    print(f"Stopping k3d cluster '{args.cluster_name}'...")
    try:
        run_command(["k3d", "cluster", "delete", args.cluster_name], check=False)
        print(f"Cluster '{args.cluster_name}' deleted.")
        if CONFIG_FILE.exists():
            try:
                os.remove(CONFIG_FILE)
                print(f"Successfully removed config file: {CONFIG_FILE}")
            except OSError as e:
                print(f"Error removing config file {CONFIG_FILE}: {e.strerror}")
    except Exception as e:
        print(f"Error stopping k3d cluster: {str(e)}")


def get(args):
    """Get resource"""
    get_argocd_password(args.namespace)


def main():
    parser = argparse.ArgumentParser(
        description="Manage a local ArgoCD environment with k3d."
    )
    subparsers = parser.add_subparsers(title="commands", dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start the ArgoCD environment")
    start_parser.add_argument(
        "--namespace", default="argocd", help="Kubernetes namespace for ArgoCD"
    )
    start_parser.add_argument(
        "--github-repo",
        help="GitHub repository in 'owner/repo_name' format (e.g., jburandt/argocd-local). If not provided, you will be prompted.",
    )
    start_parser.add_argument(
        "--setup-aks", action="store_true", default=True, help="Setup interactive AKS"
    )
    start_parser.add_argument(
        "--aks-subscription-id", help="Azure Subscription ID for AKS cluster"
    )
    start_parser.add_argument(
        "--aks-resource-group", help="Azure Resource Group of the AKS cluster"
    )
    start_parser.add_argument("--aks-cluster-name", help="Name of the AKS cluster")
    start_parser.set_defaults(func=start)

    stop_parser = subparsers.add_parser("stop", help="Stop the ArgoCD environment")
    stop_parser.add_argument(
        "--cluster-name", default="gitops-local", help="local k3d cluster name"
    )
    stop_parser.set_defaults(func=stop)

    get_parser = subparsers.add_parser("get", help="Fetch resources using the script")
    get_parser.add_argument(
        "--namespace", default="argocd", help="Kubernetes namespace for ArgoCD"
    )
    get_parser.set_defaults(func=get)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except ArgocdLocalError as e:
        print(f"Error: {e}")
        sys.exit(1)
