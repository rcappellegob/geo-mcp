FROM python:3.14-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
ENV PYTHONPATH=/app

# Mount points used by sync_urbis.py and the MCP server: the downloaded
# GeoPackage, the .env with PG*/GEO_MCP_* settings, and the host-side log
# directory are all expected to be bind-mounted at container run time (see
# run commands in the technical reference doc). Creating them here just
# avoids surprises if someone forgets to mount them.
RUN mkdir -p /srv/resources/urbis /srv/geo-mcp /srv/logs/geo-mcp

EXPOSE 8090
CMD ["python", "-m", "app.main"]
