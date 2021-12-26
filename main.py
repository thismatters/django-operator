import kopf

from django_operator.kinds import DjangoKind

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


def begin_migration(patch, **kwargs):
    patch.status["condition"] = "migrating"


async def do_migration(logger, **kwargs)
    return await DjangoKind(logger=logger).update_or_create(**kwargs)

@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
async def create_handler(logger, **kwargs):
    await kopf.execute(fns={
        "begin": begin_migration,
    })
    await kopf.execute(fns={
        "do": do_migration,
    })


# @kopf.on.timer("thismatters.net", "v1alpha", "djangos", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)
