FROM python:3.10.11 AS base

FROM scratch

COPY --from=base /usr/local/lib/python3.10 /usr/local/lib/python3.10

COPY ./http-server.py /app/http-server.py
