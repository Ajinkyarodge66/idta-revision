# Using your own website name

Swap `ajinkyarodge66.github.io/idta-revision` for a name like `idtarevision.com`.

## 1. Buy a domain (about £10 a year)

Any registrar works, for example Cloudflare Registrar, Namecheap, Porkbun or GoDaddy. Pick a short `.com`, `.co.uk` or `.app`.

## 2. Point it at GitHub (in your registrar's DNS settings)

For the bare domain (`idtarevision.com`), add four **A** records with host `@`:

```
185.199.108.153
185.199.109.153
185.199.110.153
185.199.111.153
```

For `www`, add a **CNAME** record: host `www` → value `ajinkyarodge66.github.io`.

Remove any old "parking" A records the registrar added.

## 3. Tell GitHub

1. Go to the repository, then **Settings → Pages → Custom domain**. Type `idtarevision.com` and click **Save**.
2. Wait for the DNS check to pass (minutes to a few hours), then tick **Enforce HTTPS**.

GitHub creates a `CNAME` file in the repository. The app's share button, link previews and offline mode then use the new name automatically after the next rebuild.

## 4. Keep the old link working

Links to `ajinkyarodge66.github.io/idta-revision/` redirect to the new domain automatically.

Students' saved logins are tied to the website address, so everyone logs in once more on the new domain. Their progress is safe, because it's backed up to their account.
