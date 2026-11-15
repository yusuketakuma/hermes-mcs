"""Share patient activity ordering without owning admission, fairness turns or claims."""


def latest_activity_sql(project_id_column: str) -> str:
    """Return the shared activity projection for a fixed caller-owned SQL column."""
    if project_id_column not in ('m.project_id', 'p.project_id', 'j.project_id', 'fetch_jobs.project_id'):
        raise ValueError('patient_priority_column_invalid')
    return ('(SELECT MAX(activity.posted_at_ts) FROM messages activity '
            f'WHERE activity.project_id={project_id_column})')


def activity_order_sql(project_id_column: str) -> str:
    """Order an already selected archive cohort by activity and project ID."""
    return latest_activity_sql(project_id_column) + f' DESC, {project_id_column} ASC'
