FROM python:3.7
ADD . /src
RUN pip install -r requirements.txt

CMD kopf run /src/mp_operator.py --verbose
