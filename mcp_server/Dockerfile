FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface

COPY requirements.txt .
# torch is CPU-only here (no GPU in this stack) -- pull it from PyTorch's own
# CPU wheel index so pip doesn't grab a multi-GB CUDA build.
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000
# CMD (not ENTRYPOINT) so docker-compose's `command:` can freely replace this,
# e.g. append --reload for dev. `server:app` is the ASGI app exposed at
# module level in server.py -- running it through plain uvicorn (rather than
# calling mcp.run() from __main__) is what makes --reload possible.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
