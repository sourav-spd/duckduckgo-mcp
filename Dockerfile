FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 7070
ENTRYPOINT ["python", "-m", "duckduckgo_browser"]
CMD ["--mode", "streamable-http", "--host", "0.0.0.0", "--port", "7070"]
