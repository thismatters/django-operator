# MP Operator

A [Kubernetes operator](https://github.com/cncf/tag-app-delivery/blob/main/operator-wg/whitepaper/Operator-WhitePaper_v1-0.md) for Money Positive.

Provides a `MoneyPositive` CRD which:
* Coordinates deployment of app
  * creates ingresses for app
  * creates certs for app
  * ensures one beat (`django-celery-beat`)
  * scales app and worker containers independently
* Coordinates initialization of app proper
  * handles migrations
  * handles fixtures
  * handles initialization management commands


Takes some inspiration from [21h/django-operator](https://git.blindage.org/21h/django-operator) which is presented as a freestanding operator written in `go`.


```yaml
apiVersion: blindage.org/v2
kind: Django
metadata:
  name: my-project
  namespace: default
spec:
  image: registry.gitlab.com/my-organization/project-repo/master:11
  replicas: 2
  # port opened by application inside container
  appPort: 8000
  # set some ENVs
  appEnvs:
    DJANGO_SUPERUSER_USERNAME: "root"
    DJANGO_SUPERUSER_EMAIL: root@root.xyz
    ALLOWED_HOSTS: "*"
  # get some ENVs from predefined secret resource
  appEnvsSecrets:
    - my-project-secrets
  # or you can mount ENVs from configmap
  appEnvsConfigmaps:
    - other-project-envs
  # path to static files, default /app/static
  appStaticPath: "/app/static"
  # path to media files, default /app/media, used for file uploads
  appMediaPath: "/app/media"
  # run 'python manage.py migrate --noinput' in init container before start
  runMigrate: true
  # run 'python manage.py collectstatic --noinput' in init container before start
  runCollectStatic: true
  # persistence for appMediaPath
  # use this for file uploads
  persistentVolumeClaim: supershop-disk
```

## TODO:

* [x] Update secrets in staging, prod once tested
* [x] Figure out global pattern (https://github.com/nolar/kopf/issues/876) -- monolithic!
* [] write CRD
* [x] create manifests for:
  * [x] deployment -- app
  * [x] deployment -- worker
  * [x] deployment -- beat
  * [x] deployment -- redis
  * [x] ingress -- app
  * [x] service -- redis
  * [x] service -- app
  * [x] job -- migrations
* [x] write create/update the code
* [] write delete code (shouldn't be much here really...)
* [] set up CI pipeline
* [] lint
* [] deploy to cluster (for testing)
* [] test (create a new namespace for testing, and use an arbitrary URL)
* [] deploy
