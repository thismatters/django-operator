FROM python:3.9-alpine

RUN mkdir -p /op
WORKDIR /op

COPY requirements.txt /op
RUN pip install -r /op/requirements.txt
RUN rm /op/requirements.txt

ADD ./src /op/src
ADD ./manifests /op/manifests

RUN adduser -D worker -u 1000
USER worker

CMD kopf run src/django.py --verbose
