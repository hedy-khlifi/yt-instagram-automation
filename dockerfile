FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Deno (already working, keep it)
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o deno.zip \
    && unzip deno.zip -d /usr/local/bin \
    && rm deno.zip \
    && chmod +x /usr/local/bin/deno

# Install the PO Token provider server globally
RUN npm install -g bgutil-ytdlp-pot-provider

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
# Start the PO token server in background, then start your app
CMD bgutil-pot-server & uvicorn main:app --host 0.0.0.0 --port 8000