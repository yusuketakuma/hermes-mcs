"""Share patient activity ordering without owning admission, fairness turns or claims."""


def priority_key(project_id: int | None, latest_activity: float | None, *, archived: bool = False) -> tuple:
    """Order active patients, known latest activity descending, then stable project ID."""
    return (bool(archived), latest_activity is None,
            -latest_activity if latest_activity is not None else 0,
            project_id is not None, project_id if project_id is not None else 0)


def latest_activity_sql(project_id_column: str) -> str:
    """Return the shared activity projection for a fixed caller-owned SQL column."""
    if project_id_column not in ('m.project_id', 'p.project_id', 'j.project_id', 'fetch_jobs.project_id'):
        raise ValueError('patient_priority_column_invalid')
    return ('(SELECT MAX(activity.posted_at_ts) FROM messages activity '
            f'WHERE activity.project_id={project_id_column})')


def activity_order_sql(project_id_column: str) -> str:
    """Order an already selected archive cohort by activity and project ID."""
    return latest_activity_sql(project_id_column) + f' DESC, {project_id_column} ASC'
