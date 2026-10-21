# Accounts, leaderboard & admin dashboard: setup

The backend is a Google Apps Script attached to a Google Sheet you own. It stores student accounts, saved progress and activity, sends password-reset codes from your Gmail, and powers the leaderboard and admin dashboard.

## First-time setup

1. Go to https://sheets.new, then click **Extensions → Apps Script**.
2. Replace the sample code with everything in [`Code.gs`](Code.gs) and click **Save**.
3. Set your admin password. Paste this at the bottom of the file, put your password in the quotes, choose `setAdminKey` next to **Run**, click **Run**, and then **delete these lines**:
   ```js
   function setAdminKey() { PropertiesService.getScriptProperties().setProperty('ADMIN_KEY', 'your-password'); }
   ```
4. Choose `setup` next to **Run** and click **Run**. Approve the permissions. Gmail permission is needed to email reset codes. The log shows the sheet link and "ADMIN_KEY is set."
5. Click **Deploy → New deployment → ⚙️ Web app**. Set **Execute as: Me** and **Who has access: Anyone**, then click **Deploy** and copy the `/exec` URL.
6. Put the URL in `index.html` as `const SYNC_URL = "…/exec";`.

## Private university packs (`Pack.gs`)

The University of Portsmouth IDTA material (module notes and all 219 module questions, including the 25 official sample-paper questions) is **not in this public repository**. It lives in a second script file that only your Google Script can read, and it's served only to students whose university email is verified.

1. In Apps Script, click **+ (Add a file) → Script** and name it `Pack`.
2. Paste the private `Pack.gs` you were given and click **Save**. Never commit it to GitHub or share it.
3. Students verify by entering their **@myport.ac.uk** (or @port.ac.uk) email in **My university**. They receive a 6-digit code, and once they enter it the pack unlocks.
4. In **Admin → Overview → Students**, the **Verified** column lets you **Grant** or **Revoke** access by hand.

To add another university later, add its email domain in Admin and its pack to `Pack.gs`.

## Exam access and coupon codes

Lessons, Practice, Calc Lab and flashcards are free for everyone, and verified university students always get their module exam free. The **AI & ML course exam** can be locked so students need a code to take it.

- **Admin → Exam & coupons** lets you turn the lock on or off, set the price shown (default £3), and write the "how to pay / get a code" text that logged-in students see on the lock screen.
- **Create a coupon code**: leave the code blank to generate one (e.g. `IDTA-7F3K2A`), or type your own (e.g. `WELCOME`). Choose how many students can use it and when it expires. The note is only for you, e.g. who paid.
- Students enter the code on the **Mock exam** tab, and it unlocks immediately for their account on every device.
- To unlock one student directly, for example after they pay you, use **Grant exam** in **Admin → Overview → Students**.

Codes and who used them are stored in the **Coupons** sheet. The lock is on by default once this version of `Code.gs` is deployed.

## Updating the script (keeps the same URL)

1. Paste the new `Code.gs` and click **Save**.
2. Run `setup` once. This is needed whenever the new version asks for new permissions.
3. Click **Deploy → Manage deployments → ✏️ (edit) → Version: New version → Deploy**.

Don't use "New deployment" for updates: it creates a new URL, and the app would need changing.

## Sheets it creates

| Sheet | Contents |
|---|---|
| **Users** | ID, created, name, mobile, email, university, course, password hash + salt (never the password), leaderboard choice, saved progress, last login, reset code hash, consent time, university email, verified university, verification code hash, exam access (how it was unlocked) |
| **Sessions** | Login tokens, stored only as hashes, with a 30-day expiry |
| **Events** | One row per answer, mock or Calc Lab attempt |
| **Announcements** | Banners you post from the admin page (title, message, style, expiry) |
| **Questions** | Questions you add from the admin page (IDs from 1000 up) |
| **Coupons** | Exam coupon codes: code, max uses, times used, expiry, your note, and which accounts used it |

Hidden built-in questions are saved in the `HIDDEN_Q` script property.

Optional: in Apps Script, go to **Triggers → Add trigger → `cleanSessions`, time-driven, daily** to tidy expired logins.

## Security notes

- Passwords are stretched in the browser (PBKDF2, 150,000 rounds) and hashed again on the server with a per-user salt and a secret `PEPPER`. Nobody, including you, can read a password from the sheet.
- 5 wrong logins lock that number for 15 minutes. Reset codes expire after 15 minutes, and 5 wrong codes trigger a lockout.
- Text is sanitised so nobody can inject spreadsheet formulas.
- **The Users sheet holds personal data** (mobile numbers and emails). Don't share the sheet, don't share the admin key, and delete the data when the module ends. Students can delete their own account at any time.
- Gmail allows about 100 emails a day on a free account, which is plenty for reset codes.
