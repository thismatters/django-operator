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
    labels={
        MigrationPipeline.label: lambda value, **_: MigrationPipeline.is_step_name(value)
    },
)
def migration_pipeline(**kwargs):
    return MigrationPipeline(**kwargs).handle()
