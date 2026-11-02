# GitHub failure report setup

Failure reports are sent as individual issues to a private repository configured in app Settings (default: `Krazy8-pixel/Fb_automation`). Each issue has a **Complete failure report and logs** artifact with the full payload and captured logs. Failed sends stay as separate local queue files and retry automatically. The app verifies the selected repository is private before sending.

The app uses GitHub App device authorization. It does not store a GitHub client secret in the EXE. The user access token is encrypted with Windows DPAPI on the PC. Install the GitHub App only on the report repository and grant only Issues write permission (plus required repository metadata access).

## One-time GitHub setup

The GitHub App must be created by the repository owner:

1. Open **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Name it `Tayyara Motors Failure Reporter`. Set its homepage URL to `https://github.com/Krazy8-pixel/Fb_automation`.
3. Leave webhooks disabled. Under **Repository permissions**, set **Issues** to **Read and write**. Leave other permissions at **No access** except required read-only repository metadata.
4. Under **Identifying and authorizing users**, enable **Device flow**. This flow does not use a callback URL.
5. Create the app. Under **Install App**, install it on your account and choose **Only select repositories → Fb_automation** (or choose the private repository you intend to use).
6. Copy the public **Client ID**, not a client secret. In Tayyara Motors, open **Settings → Failure reports to GitHub**, set the repository to `owner/name`, paste the Client ID, and click **Save Client ID**.
7. Click **Connect GitHub** and complete the device authorization in the browser using the code shown in the app.

After setup, failures are submitted automatically. The public Client ID can be changed in app Settings. Never enter a GitHub client secret, personal access token, or private key in the app or chat.
