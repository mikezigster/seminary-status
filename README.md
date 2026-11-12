# Seminary status page

A read-only accountability page built from my Todoist seminary projects and
published with GitHub Pages. It refreshes itself every few hours. Friends can
open the link; they don't need a Todoist or GitHub account.

## What it shows

Every class project nested under **MAC Classes** in Todoist, sorted into:

- **Overdue**: open tasks past their date, oldest first, with how many days late
- **Due this week**: today through Sunday
- **The plan ahead**: the next 90 days

When a new term starts, add the class as a project under **MAC Classes** and give
its tasks due dates. The page picks it up on the next refresh. Classes with no
dated tasks are left off automatically.

A few rules the page follows:

- Subtasks with their own dates (outline, draft, revise…) appear as steps, like
  "Response Paper 3 · step 2 of 4". Subtasks that share their parent's date are
  counted under the parent ("4 parts left").
- If a parent task is overdue, its steps are folded into it rather than listed
  one by one.
- Tasks that start with `* ` (Todoist's uncompletable headers) and undated tasks
  are skipped.
- "Watch:" tasks with `(47min)` in the name count toward lecture time behind.
- Week number and topic come from section names like
  `Week 9 (11/9-15): Prophets & Daniel`.

## One-time setup (about 10 minutes)

1. **Create the repository.** On GitHub, click **New repository**. Name it
   something like `seminary-status`, choose **Public** (GitHub Pages is free
   for public repositories), and create it.

2. **Upload these files.** On the new repository's page, click
   **uploading an existing file** and drag in everything from this folder,
   including the `.github` folder.
   *On a Mac, Finder hides folders whose names start with a dot. Press
   Cmd + Shift + . (period) in Finder to show `.github` before dragging.*
   Commit the upload.

3. **Add your Todoist token as a secret.**
   - In Todoist: **Settings → Integrations → Developer**, then copy your API token.
   - In the repository: **Settings → Secrets and variables → Actions →
     New repository secret**. Name: `TODOIST_TOKEN`. Value: paste the token.

   The token stays encrypted inside GitHub. It is never written into the page.

4. **Turn on Pages.** **Settings → Pages → Build and deployment → Source:
   GitHub Actions.**

5. **Run it once.** **Actions → Publish status page → Run workflow.** After
   about a minute, the page is live at
   `https://<your-github-username>.github.io/seminary-status/`.
   Send that link to your friends.

## Day to day

- The page refreshes about every 3 hours from 5 AM to 11 PM Central. To refresh
  it right away, use **Actions → Publish status page → Run workflow**.
- GitHub pauses scheduled jobs in a repository that has gone 60 days without
  activity. The `keepalive` workflow makes one empty commit a month so that
  doesn't happen.
- Settings live in `config.json`: the parent project name, time zone, how far
  ahead to plan, and how many overdue items show on a phone before the
  "Show all" button.

## Privacy

- Anyone with the link can see task names and dates. The page asks search
  engines not to index it, but treat the link as visible to whoever has it.
- Your Todoist token can read and change your whole Todoist account, which is
  why it lives only in the encrypted GitHub secret. To cut off access entirely,
  issue a new API token in Todoist. The old token stops working, so the page
  stops updating until you paste the new one into the secret.
- To take the page down, turn off Pages in **Settings → Pages**, or delete
  the repository.

## Testing on your own computer (optional)

```
TODOIST_TOKEN=your-token python3 build.py --out site
open site/index.html
```

Python 3.9 or newer. No packages to install.
