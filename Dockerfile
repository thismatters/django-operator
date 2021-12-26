FROM python:3.9-alpine

RUN mkdir -p /op
WORKDIR /op

COPY requirements.txt /op
RUN pip install -r /op/requirements.txt
RUN rm /op/requirements.txt

ADD ./django_operator /op/django_operator
ADD ./main.py /op/main.py
ADD ./manifests /op/manifests

RUN adduser -D worker -u 1000
USER worker
ENV PATH="/home/worker/.local/bin:${PATH}"

CMD kopf run --all-namespaces main.py --verbose
