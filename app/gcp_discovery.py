"""Discover GCP resources — list BQ datasets, GCS buckets from any GCP project.

Uses Application Default Credentials (the control panel's service account).
The service account needs:
  - BigQuery: roles/bigquery.metadataViewer on target projects
  - GCS: roles/storage.objectViewer on target projects/buckets
"""
import logging
from typing import Optional

_log = logging.getLogger(__name__)


async def discover_gcp_projects() -> list[dict]:
    """List GCP projects accessible to the control panel's service account.

    Returns: [{"project_id": "my-project", "name": "My Project", "number": "123456"}, ...]
    """
    try:
        from google.cloud import resourcemanager_v3

        client = resourcemanager_v3.ProjectsClient()
        projects = []
        for project in client.search_projects():
            if project.state.name == "ACTIVE":
                projects.append({
                    "project_id": project.project_id,
                    "name": project.display_name or project.project_id,
                    "number": project.name.split("/")[-1] if project.name else "",
                })

        _log.info("Discovered %d GCP projects", len(projects))
        return projects

    except ImportError:
        # Fallback: try using the older API
        try:
            import google.auth
            import google.auth.transport.requests
            import httpx

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"])
            credentials.refresh(google.auth.transport.requests.Request())

            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(
                    "https://cloudresourcemanager.googleapis.com/v1/projects",
                    headers={"Authorization": f"Bearer {credentials.token}"},
                    params={"filter": "lifecycleState:ACTIVE"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return [
                        {"project_id": p["projectId"], "name": p.get("name", p["projectId"]), "number": p.get("projectNumber", "")}
                        for p in data.get("projects", [])
                    ]
        except Exception as exc:
            _log.warning("GCP project discovery fallback failed: %s", exc)

        return []

    except Exception as exc:
        _log.warning("GCP project discovery failed: %s", exc)
        return []


async def discover_bigquery_datasets(gcp_project: str) -> list[dict]:
    """List BigQuery datasets in a GCP project.

    Returns: [{"name": "analytics", "table_count": 42, "location": "US"}, ...]
    """
    try:
        from google.cloud import bigquery
        client = bigquery.Client(project=gcp_project)

        datasets = []
        for ds_ref in client.list_datasets():
            ds = client.get_dataset(ds_ref.reference)
            # Count tables (quick metadata call)
            tables = list(client.list_tables(ds_ref.reference))
            datasets.append({
                "name": ds.dataset_id,
                "full_id": f"{gcp_project}.{ds.dataset_id}",
                "location": ds.location or "",
                "table_count": len(tables),
                "description": ds.description or "",
                "created": str(ds.created) if ds.created else "",
            })

        _log.info("Discovered %d BQ datasets in %s", len(datasets), gcp_project)
        return datasets

    except ImportError:
        _log.error("google-cloud-bigquery not installed — run: pip install google-cloud-bigquery")
        return []
    except Exception as exc:
        _log.warning("BQ discovery failed for %s: %s", gcp_project, exc)
        return []


async def discover_bigquery_tables(gcp_project: str, dataset: str) -> list[dict]:
    """List tables in a BigQuery dataset.

    Returns: [{"name": "events", "type": "TABLE", "row_count": 1000000}, ...]
    """
    try:
        from google.cloud import bigquery
        client = bigquery.Client(project=gcp_project)

        tables = []
        for table_ref in client.list_tables(f"{gcp_project}.{dataset}"):
            table = client.get_table(table_ref.reference)
            tables.append({
                "name": table.table_id,
                "full_id": f"{gcp_project}.{dataset}.{table.table_id}",
                "type": table.table_type or "TABLE",
                "row_count": table.num_rows or 0,
                "size_bytes": table.num_bytes or 0,
                "description": table.description or "",
            })

        _log.info("Discovered %d tables in %s.%s", len(tables), gcp_project, dataset)
        return tables

    except ImportError:
        return []
    except Exception as exc:
        _log.warning("BQ table discovery failed for %s.%s: %s", gcp_project, dataset, exc)
        return []


async def discover_gcs_buckets(gcp_project: str) -> list[dict]:
    """List GCS buckets in a GCP project.

    Returns: [{"name": "my-bucket", "location": "US", "storage_class": "STANDARD"}, ...]
    """
    try:
        from google.cloud import storage
        client = storage.Client(project=gcp_project)

        buckets = []
        for b in client.list_buckets():
            buckets.append({
                "name": b.name,
                "location": b.location or "",
                "storage_class": b.storage_class or "",
                "created": str(b.time_created) if b.time_created else "",
            })

        _log.info("Discovered %d GCS buckets in %s", len(buckets), gcp_project)
        return buckets

    except Exception as exc:
        _log.warning("GCS bucket discovery failed for %s: %s", gcp_project, exc)
        return []


async def discover_gcs_contents(
    gcp_project: str, bucket_name: str, prefix: str = "", max_results: int = 100,
) -> dict:
    """List contents of a GCS bucket (files + folder prefixes).

    Returns: {"prefixes": ["designs/", "data/"], "files": [{"name": "readme.txt", "size": 1234}, ...]}
    """
    try:
        from google.cloud import storage
        client = storage.Client(project=gcp_project)
        bucket = client.bucket(bucket_name)

        # Use delimiter to get folder-like listing
        blobs_iter = bucket.list_blobs(prefix=prefix, delimiter="/", max_results=max_results)
        files = []
        for blob in blobs_iter:
            files.append({
                "name": blob.name,
                "size_bytes": blob.size or 0,
                "content_type": blob.content_type or "",
                "updated": str(blob.updated) if blob.updated else "",
            })

        prefixes = list(blobs_iter.prefixes) if hasattr(blobs_iter, 'prefixes') else []

        _log.info("Discovered %d files + %d prefixes in gs://%s/%s",
                   len(files), len(prefixes), bucket_name, prefix)
        return {"prefixes": prefixes, "files": files}

    except Exception as exc:
        _log.warning("GCS content discovery failed for gs://%s/%s: %s", bucket_name, prefix, exc)
        return {"prefixes": [], "files": []}
