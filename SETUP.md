# Coin Compass: setup (about 15 minutes)

You need: your GitHub account, a laptop for the upload step (easiest), and your S24 Ultra.

## 1. Put the files on GitHub
1. On github.com tap **New repository**. Name it `coin-compass`. Set it to **Public** (needed for free GitHub Pages). Create it.
2. Click **uploading an existing file**. On a laptop, unzip `coin-compass.zip` and drag in ALL the files and the `.github` folder.
   - Hidden folder tip: if `.github` does not show up in your file picker, press Ctrl+Shift+. (Windows) or Cmd+Shift+. (Mac) to show hidden files.
3. Click **Commit changes**.

## 2. Turn on the app (GitHub Pages)
Repo **Settings > Pages > Source: Deploy from a branch > Branch: main, folder / (root) > Save**.
After about a minute your app is live at `https://YOUR-USERNAME.github.io/coin-compass/`.

## 3. Install it on your S24 Ultra
Open that address in Chrome > menu (three dots) > **Add to Home screen** (or **Install app**). It now opens full screen like a normal app.
In the app go to **Alerts > Turn on phone notifications** and allow.

## 4. Alerts when the app is closed (free)
1. Install the **ntfy** app from the Play Store. Tap **+** and subscribe to a topic name only you would guess, for example `maish-compass-4821`.
2. In your repo: **Settings > Secrets and variables > Actions**
   - Secrets tab > **New repository secret**: name `NTFY_TOPIC`, value = your topic name.
   - Variables tab > **New repository variable**: name `APP_URL`, value = your app address from step 2 (tapping an alert opens the app).
3. Repo **Actions** tab > **coin-compass-alerts** > **Run workflow**. Within a minute you should get a first daily brief on your phone.
4. In the app, **Alerts > Send test** also checks that ntfy works.

From then on it checks every hour. Times can slip a few minutes. The brief goes out once a day after 06:00 UTC. Change `brief_hour_utc` in `config.json` to move it.

## Changing your coins or price alerts
Adopting a coin or adding a price alert in the app only changes your phone. To make the cloud job use them too: **Alerts > Copy my settings for the cloud job**, then open `config.json` on GitHub (pencil icon), paste, and commit.

## If alerts stop
GitHub pauses scheduled jobs on repos with no activity for 60 days. The job saves its state about once a day, which normally keeps it alive. If it ever stops, open the Actions tab and click **Run workflow**.

## Good to know
- Prices come from CoinGecko, history and news from CryptoCompare, market mood from alternative.me. All free, with rate limits. If data fails to load, tap refresh after a minute.
- Signals are rules-based information, not predictions or financial advice.
