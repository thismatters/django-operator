import kopf

from django_operator.pipelines.migration import (
    MigrationPipeline,
    MonitorException,
)


@kopf.on.create("thismatters.github", "v1alpha", "djangos")
def initial_migration(patch, body, **kwargs):
    kopf.info(body, reason="Migrating", message="Enacting brand new config")
    patch.metadata.labels[MigrationPipeline.label] = MigrationPipeline.steps[0].name


# catch-all update handler
@kopf.on.update(
    "thismatters.github",
    "v1alpha",
    "djangos",
    labels={MigrationPipeline.label: MigrationPipeline.is_step_name},
)
def migration_pipeline(**kwargs):
    return MigrationPipeline(**kwargs).handle()


# catch-all update handler
@kopf.on.delete("thismatters.github", "v1alpha", "djangos")
def unprotect_resources(**kwargs):
    MigrationPipeline(**kwargs).unprotect_all()


@kopf.daemon(
    "thismatters.github",
    "v1alpha",
    "djangos",
    labels={MigrationPipeline.label: MigrationPipeline.waiting_step_name},
)
def monitor_resources(stopped, **kwargs):
    """Watch the `created` resources to ensure that they are still present.

    Trigger the migration process if anything is missing.
    """
    logger = kwargs.get("logger")
    while not stopped:
        try:
            MigrationPipeline(**kwargs).monitor()
        except MonitorException:
            logger.debug("monitor_resources found a problem, stopping.")
            raise kopf.TemporaryError("Need to restart.", delay=10)
        stopped.wait(120)
    logger.debug("monitor_resources daemon is stopping...")
