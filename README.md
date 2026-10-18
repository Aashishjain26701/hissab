# AHARIVA Hissab

A small, password-protected bookkeeping dashboard for AHARIVA Nuts. It uses a Python/Flask backend and SQLite. Amounts are entered in INR; the database stores paise.

## Run locally

1. Install Python 3.10+.
2. In this folder, create a virtual environment and install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Set a private app password and signing key in the current PowerShell window:

   ```powershell
   $env:APP_PASSWORD = "choose-a-long-private-password"
   $env:APP_SECRET_KEY = "choose-a-long-random-secret"
   python app.py
   ```

   Do not use values from `.env.example` as credentials. Generate unique values
   for your installation and keep them out of source control.

4. Open http://127.0.0.1:5000 and sign in with `APP_PASSWORD`.

The SQLite database is created at `instance/hissab.db`. Back it up regularly. Keep this database file private.

## Bookkeeping behavior

- Add products with opening stock, average unit cost, selling price and GST rate. Purchases increase stock and update weighted average cost; sales reduce stock and record cost of goods sold.
- Sales can be tagged Local, Online - direct, or Marketplace. Marketplace proceeds can be recorded to Marketplace Clearing and later transferred to your bank. Enter fees and return shipping as separate expenses.
- Customer returns reduce revenue and GST. Select the restock option only if the product is saleable; returned stock then increases inventory.
- Expenses include packaging material, return shipping, delivery, marketplace fees and other costs. Mark GST input credit eligible only when supported by valid tax records and confirmed by your accountant.
- Partner contributions and withdrawals are tracked by partner. Transfers between accounts change balances but are not revenue or expenses.
- Opening balances set each account's start-of-book balance. Setting one replaces its previous opening balance; it does not change activity entries.
- The GST card is a bookkeeping estimate: recorded output GST less recorded eligible input GST. It is not a filed return, tax advice, or an automatic recommendation to transfer money. A bank transfer to Savings is entered explicitly as an account transfer.
- Operating profit uses net sales less cost of sold inventory and recorded expenses. It is a simple management view, not a full statutory P&L or balance sheet.

## Deployment

Production requires `APP_ENV=production`, a unique strong `APP_PASSWORD`, a persistent random `APP_SECRET_KEY`, and a PostgreSQL `DATABASE_URL`. Production startup refuses to fall back to local SQLite. The app requires SSL for PostgreSQL connections in production and marks session cookies Secure, so serve it over HTTPS.

### No-cost trial deployment

The included `render.yaml` can create a free Render web service. For its database, use a separate Neon PostgreSQL project and set its connection string as the Render `DATABASE_URL` secret. Do **not** use Render's free PostgreSQL database for bookkeeping: Render documents that its free databases expire after 30 days and have no backups.

1. Push this project to a private GitHub repository; do not commit `.env` files, passwords, keys, or database files.
2. Create a Neon project on its Free plan, then copy its pooled or direct PostgreSQL connection string. Keep it private.
3. In Render, create a Blueprint from the repository and use `render.yaml`. Enter a unique `APP_PASSWORD` and the Neon connection string when prompted for the unsynced environment variables. Render generates `APP_SECRET_KEY`.
4. After deployment, test sign-in, create test records, restart/redeploy, and verify the records remain. Delete the test records before using the app for real books.

**Free hosting is not production-grade storage.** Render says Free web services are not for production, sleep when idle, and have ephemeral filesystems. Neon Free is intended for prototypes and has usage/storage limits and no scheduled backups. Free services may be suspended or changed by providers; there is no uptime or data durability guarantee. Keep verified off-platform backups and do not rely on this free setup as the only copy of live financial records. For business-critical use, pay for a host/database plan with backups and a recovery guarantee. Review the GST/accounting workflow with your accountant.

## Scope

This is a bookkeeping MVP, not a GST filing product or a substitute for an accountant. It does not yet reconcile bank statements, track supplier/customer balances, handle GST slabs per invoice line, or integrate ecommerce orders automatically.
