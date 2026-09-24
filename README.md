

## Private hosted version

The browser app is upload-only. Each person uploads:

1. A new ServiceTitan export
2. Their current master dataset
3. Their current new-leads-only dataset

The app creates download-ready versions of the cleaned export, updated master, updated new-leads-only dataset, and period comparison. It does not write client files into this project or keep a permanent copy on the server.

### Deploy privately

1. Put this project in a **private GitHub repository**. Do not upload client CSV or XLSX files.
2. Create a Streamlit Community Cloud account and connect it to GitHub.
3. Choose **Create app**, select the private repository, and select `app.py`.
4. In the app Sharing settings, select **Only specific people can view this app**.
5. Invite the approved users by email.

Each user signs in, uploads their own files, and downloads their updated copies before leaving the page.

