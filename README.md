# Canal Booker

Books Canal Building study rooms on the Carleton booking portal (booking.carleton.ca) the second they open. A whole day opens at exactly midnight, one week ahead. Everyone books under their own Carleton account, so the group can cover more than one person's 3 hour daily limit.

Your rooms and times come from the group's **team plan sheet**. Add one row per day you want: your MyCarletonOne username, the day, start and end time, optional backup times, and rooms in order of preference.

Pick **one** way to run it:

## Option A: in the cloud (no download, computer can be off)

Uses free GitHub Actions. You need a GitHub account.

1. Click **Fork** at the top of this page, then **Create fork**.
2. In your fork, open **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:
   * `CARLETON_USERNAME`: your MyCarletonOne username (the part before @carleton.ca)
   * `CARLETON_PASSWORD`: your password
3. Open the **Actions** tab and click **I understand my workflows, go ahead and enable them**.
4. Test it: **Actions → Book my room → Run workflow**, leave mode on `test`, click **Run workflow**. After a couple of minutes, open the run. It should say "Midnight mode dry run: … Nothing was booked."

That's it. Every night it starts in the evening (GitHub can start scheduled jobs hours late, so it starts early and waits), gets ready a couple of minutes before midnight, and books the moment the day opens.

**Checking that it worked:** open **Actions**, click last night's **Book my room** run, and read the box at the top: **✅ Booked Wed Oct 7** with the room and time, **❌ Not booked**, **❌ Sign-in failed** or **❌ Not set up**. Anything with ❌ counts as a failed run, and GitHub emails you about it. To get an email for successful bookings too, go to your GitHub **Settings → Notifications → Actions** and untick "Only notify for failed workflows". The portal's **My Bookings** page always shows what you actually have.

**Make the start time reliable (optional, recommended):** GitHub's own schedule can be hours late. A free outside timer can start the run at an exact time instead:
1. On GitHub: your picture → **Settings → Developer settings → Fine-grained tokens → Generate new token**. Pick only your canal-booker repository, set **Actions** to **Read and write**, and copy the token.
2. On [cron-job.org](https://cron-job.org) (free), create a job for every day at **11:40 pm** (Toronto time) with:
   * URL: `https://api.github.com/repos/YOUR-GITHUB-NAME/canal-booker/actions/workflows/book.yml/dispatches`
   * Method **POST**, body `{"ref":"main","inputs":{"mode":"book"}}`
   * Headers: `Authorization: Bearer YOUR-TOKEN`, `Accept: application/vnd.github+json`
3. The token can only run your repo's workflows. It can't read your password secret.

Good to know:
* Your password is stored as an encrypted GitHub secret and is never shown in logs. Run logs of a public fork can be seen by others, and they show which room and time you booked.
* GitHub turns off scheduled runs in a repo with no activity for 60 days. If you get an email about that, click to turn it back on.
* GitHub's rules say Actions is for building and testing software, so they could turn off a workflow used like this. If that happens, use Option B.

## Option B: on your computer

1. Go to **Releases** (right side of this page) and download `CanalBooker.exe` (Windows) or `CanalBooker-Mac.zip` (Mac).
   * Windows: double click it. If you see "Windows protected your PC", click **More info → Run anyway**.
   * Mac: unzip, move it to Applications, then right click it and choose **Open** (only the first time).
2. A page opens in your browser with a 4 step setup that ticks itself off: sign in, get your slots from the sheet, dry run, turn on.

Keep the computer plugged in, awake and online at midnight. You need Chrome or Edge (every Windows computer has Edge).

Don't use both options for the same account.

## How it books

About 2.5 minutes before midnight it signs in and opens a tab for each room and time choice (up to 6), each filled in up to the calendar. It reads the portal's clock, and from 3 seconds before midnight every tab re-checks the calendar every 1.5 seconds. The first choice that opens is booked, usually within a couple of seconds. If someone grabs your first choice, the next one is already on screen. If nothing opened, it keeps retrying for 20 minutes.

Please cancel any booking you won't use through the portal. Unused rooms block other students, and the portal shows who booked them.

## For the organizer

* **Team plan:** a Google Sheet with a header row `Name, Username, Day, Start, End, Backup 1, Backup 2, Room 1, Room 2, Room 3` (the older `Backup times` / `Rooms (in order)` lists still work). Running **Canal Booker → Set up dropdowns** in the sheet (from `sheet/Code.gs`) converts an older plan and adds the dropdowns. Rooms are per row, so each day can have its own rooms. Rows above the header are ignored, so instructions can go there. Share it as **Anyone with the link: Viewer**, and add the group as Editors. Its link is `team_plan_url` in `app_defaults.json` (the cloud version can override it with a `TEAM_PLAN_URL` variable).
* **Setup guide and Status tabs in the sheet:** in the sheet, open **Extensions → Apps Script**, paste [`sheet/Code.gs`](sheet/Code.gs), save, and run `setup` once (allow access). Then **Deploy → New deployment → Web app**, execute as **Me**, access **Anyone**, and put the `/exec` link in `app_defaults.json` as `status_url`. Every run (cloud or computer) then adds its result to the Status tab: Booked, Not booked, Sign-in failed, Test passed. Everyone's copy picks up the link from this repo, even forks made earlier. The script only accepts usernames that are in the plan and never sees passwords.
* **Booking steps** live in `portal_recipe.json`. Every copy of the app, and every fork, downloads the newest one from `recipe_url` in `app_defaults.json`, so a fix here reaches everyone without reinstalling. If the portal changes: run a dry run, see where the screenshot stopped, fix the step, raise `"version"` by 1 and commit. `record_booking_windows.bat` / `record_booking_mac.command` record a real booking to help.
* **New downloads:** push a tag like `v1.2`, or run **Actions → Build downloads → Run workflow** with the tag typed in. GitHub builds the Windows and Mac files and attaches them to a release.
* **Run from source:** Python 3.10+, then `start_windows.bat` / `start_mac.command`, or `pip install -r requirements.txt` and `python run.py`. The page is at http://127.0.0.1:5057.
