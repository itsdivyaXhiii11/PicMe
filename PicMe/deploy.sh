#!/bin/bash
# Deploy script for Oracle Cloud Free Tier
# Run this on your Oracle Cloud instance

set -e

echo "=== PicMe Deployment Script ==="

# Update system
sudo apt update
sudo apt install -y python3-pip python3-venv git nginx

# Create app directory
APP_DIR="$HOME/picme-app"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Clone repo (replace with your repo URL)
if [ ! -d "PicMe" ]; then
    git clone https://github.com/itsdivyaXhiii11/PicMe.git
fi

cd PicMe/PicMe

# Setup Python environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Create data directory
mkdir -p data/thumbs

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo ""
    echo "WARNING: Please edit .env and add your DATABASE_URL and other secrets!"
    echo "Run: nano $PWD/.env"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit .env file: nano $PWD/.env"
echo "2. Add your Supabase DATABASE_URL"
echo "3. Add ADMIN_TOKEN"
echo "4. Run: ./start.sh"
echo ""