import kopf

from kinds import DjangoKind

# The useful page
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md


@kopf.on.update("thismatters.github", "v1alpha", "django")
@kopf.on.create("thismatters.github", "v1alpha", "django")
async def created(**kwargs):
    return await DjangoKind().update_or_create(**kwargs)


# @kopf.on.timer("thismatters.net", "v1alpha", "django", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)
