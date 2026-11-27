# Oracle Cloud Deployment Guide

## Prerequisites

- Oracle Cloud Free Tier account (requires credit card for verification)
- Supabase project with photos already indexed
- GitHub repo pushed with latest code

## Step 1: Create Oracle Cloud Instance

1. Log in to Oracle Cloud Console
2. Go to **Compute** → **Instances** → **Create Instance**
3. Name: `picme-server`
4. Shape: **VM.Standard.A1.Flex** (ARM)
   - OCPUs: 2
   - Memory: 12 GB
   - Make sure "Always Free" badge is visible
5. Image: **Canonical Ubuntu 22.04** (or latest LTS)
6. Networking: Create new VCN with public subnet
7. Add SSH key: Generate new or upload your public key
8. Click **Create**

## Step 2: Connect to Instance

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@YOUR_INSTANCE_IP
```

## Step 3: Run Deployment Script

```bash
# Download and run deploy script
curl -O https://raw.githubusercontent.com/itsdivyaXhiii11/PicMe/main/PicMe/deploy.sh
chmod +x deploy.sh
./deploy.sh
```

Or manually:

```bash
# Install dependencies
sudo apt update && sudo apt install -y python3-pip python3-venv git

# Clone repo
git clone https://github.com/itsdivyaXhiii11/PicMe.git
cd PicMe/PicMe

# Setup environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create directories
mkdir -p data/thumbs

# Create .env file
cp .env.example .env
nano .env  # Add your secrets
```

## Step 4: Configure Environment Variables

Edit `.env`:

```bash
nano /home/ubuntu/picme-app/PicMe/PicMe/.env
```

Add:
```
DATABASE_URL=postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres
ADMIN_TOKEN=your-secure-random-token
THRESHOLD=0.45
SESSION_SECONDS=3600
ALLOW_SELFIE_UPLOAD=0
```

## Step 5: Start the App

### Option A: Manual Start

```bash
cd ~/picme-app/PicMe/PicMe
./start.sh
```

### Option B: Systemd Service (Recommended)

```bash
# Copy service file
sudo cp picme.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable auto-start
sudo systemctl enable picme

# Start service
sudo systemctl start picme

# Check status
sudo systemctl status picme

# View logs
sudo journalctl -u picme -f
```

## Step 6: Open Firewall Port

```bash
# Allow port 8000
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT

# Or using Oracle Cloud Security List:
# Go to Console → Networking → Virtual Cloud Networks → Your VCN
# → Security Lists → Add Ingress Rule
#   - Source CIDR: 0.0.0.0/0
#   - Destination Port: 8000
```

## Step 7: Create HTTPS Tunnel (Cloudflare)

```bash
# Install cloudflared
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared-linux-amd64
sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared

# Create tunnel
cloudflared tunnel --url http://localhost:8000
```

Share the HTTPS URL with students!

## Monitoring

```bash
# Check memory usage
free -h

# Check CPU usage
htop

# Check app logs
sudo journalctl -u picme -f

# Check if service is running
sudo systemctl status picme
```

## Performance Tuning

### Adjust Workers (if needed)

Edit `start.sh` or service file:
- 2 OCPUs → 4 workers (default)
- If CPU is maxed → reduce to 2-3 workers
- If memory is fine but CPU low → increase to 6 workers

### Database Pool Size

Already set to 2 per worker (8 total). If Supabase complains:
```bash
# In .env
POOL_SIZE=1
```

## Troubleshooting

### App won't start
```bash
# Check logs
sudo journalctl -u picme -n 50

# Check if port is in use
sudo lsof -i :8000
```

### Out of memory
```bash
# Reduce workers
export WORKERS=2
./start.sh
```

### Database connection errors
- Check DATABASE_URL in .env
- Verify Supabase is active
- Check Supabase connection limit

## After the Event

```bash
# Stop service
sudo systemctl stop picme

# Or terminate the instance to save resources
# (Oracle Cloud free tier doesn't charge, but good practice)
```

## Expected Performance

With 2 OCPU + 12GB RAM + 4 workers:
- **Concurrent face scans**: 4 at a time
- **Queue handling**: 300 people in ~15-20 minutes
- **Response time**: 1-3 seconds per scan
- **Memory usage**: ~2-3 GB total
- **CPU usage**: Spikes during scans, idle otherwise