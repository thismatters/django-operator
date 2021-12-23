import kopf

from django_operator.kinds import DjangoKind

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
async def created(logger, **kwargs):
    return await DjangoKind(logger=logger).update_or_create(**kwargs)


# @kopf.on.timer("thismatters.net", "v1alpha", "djangos", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)
