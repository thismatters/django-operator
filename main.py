import kopf

from django_operator.kinds import DjangoKind

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
def begin_migration(patch, **kwargs):
    """This feels like a little hack. All handlers will run, this one should
    run quickly just to set the status."""
    patch.status["condition"] = "migrating"


@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
async def create_handler(logger, **kwargs):
    return await DjangoKind(logger=logger).update_or_create(**kwargs)


"""Red -> green -> blue"""
@kopf.on.update("thismatters.github", "v1alpha", "djangos", labels={"migration-state": "red"})
def to_green(patch, **kwargs):
    patch.metadata.labels["migration-state"] = "green"


@kopf.on.update("thismatters.github", "v1alpha", "djangos", labels={"migration-state": "green"})
def to_blue(patch, **kwargs):
    patch.metadata.labels["migration-state"] = "blue"


# @kopf.on.timer("thismatters.net", "v1alpha", "djangos", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)
