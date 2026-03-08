# Flat Radar AWS Lightsail Deployment Guide

This guide covers setting up the Flat Radar bot on an AWS Lightsail instance (2 vCPU, 1GB RAM - $12/month bundle).

## 1. Local Preparation (On your Mac)

Before moving to the server, you need to generate Facebook session cookies and your Telegram keys.

### A. Generate Telegram Keys

1. Open the Telegram app, search for **BotFather** (the official bot with a blue tick).
2. Send `/newbot`, give it a name and a username.
3. BotFather will give you a **Bot Token** (e.g., `123456789:ABCDEF...`). Copy this.
4. Send a message to your new bot in Telegram (e.g., "hello").
5. Visit this URL in your web browser: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
6. Look for the `"chat":{"id": 1234567}` part. That `1234567` (or similar number) is your **Chat ID**. Copy this.

### B. Generate Facebook Cookies

Since the server has no graphical interface, it is much easier to login to Facebook on your Mac and copy the session.

1. On your Mac, inside the `flat-radar` project folder, run:
   ```bash
   python scraper.py --login
   ```
2. Log in to Facebook when the browser opens, ensure Marketplace loads, and press `ENTER` in your terminal.
3. This will create a `cookies.json` file in your project folder. Keep it safe.

---

## 2. Setting Up AWS Lightsail

1. Go to the AWS Lightsail console.
2. Click **Create instance**.
3. **Location**: Choose a region close to you (e.g., Mumbai `ap-south-1`).
4. **Instance image**: Select **Linux/Unix** -> **OS Only** -> **Ubuntu 22.04 LTS**.
5. **Instance plan**: Select the **$12/month** bundle (1 GB RAM, 2 vCPUs).
6. **Name**: Name your instance (e.g., `flat-radar-bot`) and click **Create instance**.
7. Wait a minute or two for the instance state to become _Running_.

---

## 3. Server Configuration

Connect to your instance using the **Connect using SSH** button in the Lightsail console, or via your terminal.

### A. Install Dependencies

Run the following commands on the server:

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install Python, pip, virtual environment, and tmux
sudo apt install python3 python3-pip python3-venv git tmux -y
```

### B. Clone the Repository

Clone your code onto the server (replace the URL with your actual Git repository):

```bash
git clone <your-repo-link>
cd flat-radar
```

_(Note: If your repo is private, or if you just want to copy files directly instead of using Git, use `scp` from your Mac to copy the entire folder up to the server)._

### C. Set Up Python Environment

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install required Python packages
pip install -r requirements.txt

# Install Playwright browser and its system dependencies
playwright install chromium
playwright install-deps
```

---

## 4. Secure Configuration & Running the Bot

Instead of hardcoding your Telegram tokens into `config.json`, we will use Environment Variables to keep them secure.

### A. Transfer Local Files

You need to get your `config.json` and `cookies.json` to the server. You can use SSH/SCP from your Mac:

```bash
scp -i path/to/LightsailDefaultKey-ap-south-1.pem config.json cookies.json ubuntu@<YOUR_SERVER_IP>:~/flat-radar/
```

_(Make sure `config.json` does NOT contain your real Telegram bot token and chat ID if you are committing it to Git)._

### B. Setting Secure Environment Variables on the Server

We will edit the bottom of your server user profile (`~/.bashrc`) so that the Telegram keys are loaded securely every time.

1. On the server, open your bash profile:
   ```bash
    nano ~/.bashrc
   ```
2. Scroll to the very bottom and add these lines (replace with your actual keys):
   ```bash
   export TELEGRAM_BOT_TOKEN="your_bot_token"
   export TELEGRAM_CHAT_ID="your_chat_id"
   ```
3. Save (`Ctrl+O`, `Enter`) and Exit (`Ctrl+X`).
4. Reload the profile:
   ```bash
   source ~/.bashrc
   ```

### C. Running the Bot 24/7 with `tmux`

Since you want the bot to run continuously even when you close your SSH connection, we use `tmux`.

1. Start a new background session:
   ```bash
   tmux new -s flatbot
   ```
2. Navigate to your project and activate the environment:
   ```bash
   cd ~/flat-radar
   source venv/bin/activate
   ```
3. Start the scheduler:
   ```bash
   python scheduler.py
   ```
4. **Detach** from the session securely so it keeps running in the background:
   Press exactly: `Ctrl+B`, release both, then press `D`.

You can now close your terminal window; the bot will wake up every few hours, scrape, and end you Telegram messages.

---

### D. Managing the Bot Later

- **To view logs or stop the bot:** SSH into the server and type:
  ```bash
  tmux attach -t flatbot
  ```
  (To stop it, just press `Ctrl+C`).
- **To test your Telegram connection immediately:** (While inside the venv)
  ```bash
  python notifier.py
  ```
