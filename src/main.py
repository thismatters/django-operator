import kopf

from django_operator.pipelines.migration import MigrationPipeline


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
    while not stopped:
        MigrationPipeline(**kwargs).monitor()
        stopped.wait(20)
    logger = kwargs.get("logger")
    logger.debug("monitor_resources daemon is stopping...")
