"""Credential-free GCP and GKE environment bootstrap contracts."""

from app.bootstrap.models import DeploymentProfile, load_deployment_profile

__all__ = ["DeploymentProfile", "load_deployment_profile"]
