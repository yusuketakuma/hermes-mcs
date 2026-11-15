"""Reference ordering the patient-priority SQL must reproduce."""


def priority_key(project_id, latest_activity, *, archived=False):
    """Active patients first, known latest activity descending, then stable project ID."""
    return (bool(archived), latest_activity is None,
            -latest_activity if latest_activity is not None else 0,
            project_id is not None, project_id if project_id is not None else 0)
