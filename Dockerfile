FROM python:3.10-slim

# Install system dependencies, Chromium, and Chromedriver
RUN apt-get update && apt-get install -y \
    wget unzip curl gnupg \
    fonts-liberation libnss3 libxss1 libasound2 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libvulkan1 xdg-utils xvfb x11-utils \
    chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Environment variables so Selenium can find Chrome and Chromedriver
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run your app (headless via xvfb)
CMD ["xvfb-run", "-a", "python3", "start_automated_trading.py"]
