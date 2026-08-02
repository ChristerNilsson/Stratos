# Stratos

Detta repo innehåller filer för kribban.se (Python-app bakom Nginx) samt statiska sidor för a.kribban.se och b.kribban.se.

## Publicering till STRATO

När ändringar pushas till huvudgrenen `main` synkroniserar GitHub Actions ändrade webbplatsfiler till din STRATO VPS via rsync över SSH. En pågående äldre deploy avbryts när en ny startar.

### Nödvändiga GitHub-hemligheter

Skapa dessa hemligheter i GitHub Repository Settings > Secrets and variables > Actions:

- `SSH_HOST` — serverns IP eller host (t.ex. `212.227.48.64`)
- `SSH_USERNAME` — användaren på VPS (t.ex. `root` eller en deploy-användare)
- `SSH_PASSWORD` — SSH-lösenordet om du använder lösenordsautentisering

Alternativt kan du använda nyckelbaserad autentisering:

- `SSH_PRIVATE_KEY` — din privata SSH-nyckel (hela nyckeltexten)

Säkerhets- och serverinställningar för lösenordsinloggning:

- I `/etc/ssh/sshd_config` måste `PasswordAuthentication yes` vara satt och sedan starta om SSH: `sudo systemctl restart sshd`.
- Öppna brandväggsporten (vanligtvis `22`) om den är stängd.
- Lösenordsautentisering är mindre säker än nyckelbaserad — rekommenderat: använd `SSH_PRIVATE_KEY` om möjligt.

Valfritt som variabler:

- `SSH_PORT` — (standard `22`)
- `SSH_REMOTE_DIR` — målmappen på servern (t.ex. `/var/www/kribban.se`)

## Var hamnar filerna?

Workflowen i `.github/workflows/deploy.yml` synkroniserar de filer som behövs i drift till målmappen som anges i `SSH_REMOTE_DIR`. Om denna variabel inte är satt används standarden `/var/www/kribban.se`. Virtuell miljö, sessionsnyckel, databas, Git-metadata och utvecklingsdokument undantas och bevaras på servern. Python-beroenden installeras endast när `requirements.txt` har ändrats.

Det betyder att filerna placeras så här på servern:

- `/var/www/kribban.se/index.html`
- `/var/www/kribban.se/a/index.html`
- `/var/www/kribban.se/b/index.html`

Om du inte ser `index.html` på servern:

1. Kontrollera att GitHub Actions-run lyckades utan fel.
2. Kontrollera att `SSH_REMOTE_DIR` pekar på samma katalog som din Nginx-root använder.
3. Kontrollera serverns kataloginnehåll med SSH:

```bash
ssh $SSH_USERNAME@$SSH_HOST "ls -l /var/www/kribban.se /var/www/kribban.se/a /var/www/kribban.se/b"
```

4. Kontrollera Nginx-konfigurationen så att den använder samma rotmapp.

Om Nginx fortfarande visar "Welcome to nginx!" betyder det ofta att den kör standardserverblocket, inte ditt site-block.

## DNS och subdomäner

 Se till att följande A-poster pekar mot IP-adressen 212.227.48.64:

- kribban.se
- a.kribban.se
- b.kribban.se

I STRATO-panelen ska de tre domänerna vara kopplade till samma webbutrymme, där rotmappen innehåller index.html och underkatalogerna a/ respektive b/ används för subdomänerna.

Workflowen i `.github/workflows/deploy.yml` använder rsync över SSH och förväntar sig ovan secrets. `rsync` måste finnas både på GitHub-runnern och VPS:en. För att skapa `SSH_PRIVATE_KEY_B64` i GitHub: base64-koda den privata SSH-nyckeln och spara resultatet som en Secret.

Om du ser "Welcome to nginx!" betyder det att domänen når servern men att servern fortfarande använder sin standardvhost. Då måste du lägga till en egen vhost för respektive domän. Exempelkoden finns i [nginx-vhost.conf](nginx-vhost.conf).
