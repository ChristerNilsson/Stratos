# Stratos

Detta repo innehåller den statiska webbplatsen för kribban.se, a.kribban.se och b.kribban.se.

## Publicering till STRATO

När ändringar pushas till huvudgrenen main kommer GitHub Actions automatiskt att ladda upp webbplatsen till STRATO med FTP.

### Nödvändiga GitHub-hemligheter

Skapa dessa hemligheter i GitHub Repository Settings > Secrets and variables > Actions:

- STRATO_FTP_HOST
- STRATO_FTP_USERNAME
- STRATO_FTP_PASSWORD

Valfritt:

- STRATO_FTP_PORT (standard 21)
- STRATO_FTP_SERVER_DIR (standard /)

Eftersom du använder en STRATO VPS Linux rekommenderas SSH/SCP istället för FTP. De GitHub Secrets du bör lägga in är:

- `SSH_HOST` — serverns IP eller host (t.ex. 212.227.48.64)
- `SSH_USERNAME` — användaren på VPS (t.ex. `root` eller din deploy-användare)
- `SSH_PRIVATE_KEY` — din privata SSH-nyckel (hela nyckeltexten)

Alternativ: lösenordsbaserad autentisering

- `SSH_PASSWORD` — om du vill använda lösenordsautentisering istället för nyckel. Workflow-exemplet i `.github/workflows/deploy.yml` kan använda `SSH_PASSWORD` (lösenord) istället för `SSH_PRIVATE_KEY`.

Säkerhets- och serverinställningar för lösenordsinloggning:

- I `/etc/ssh/sshd_config` måste `PasswordAuthentication yes` vara satt och sedan starta om SSH: `sudo systemctl restart sshd`.
- Öppna brandväggsporten (vanligtvis 22) om den är stängd.
- Lösenordsautentisering är mindre säker än nyckelbaserad — rekommenderat: använd `SSH_PRIVATE_KEY` om möjligt.

Valfritt som variabler:

- `SSH_PORT` — (standard `22`)
- `SSH_REMOTE_DIR` — målmappen på servern (t.ex. `/var/www/kribban.se`)

## DNS och subdomäner

 Se till att följande A-poster pekar mot IP-adressen 212.227.48.64:

- kribban.se
- a.kribban.se
- b.kribban.se

I STRATO-panelen ska de tre domänerna vara kopplade till samma webbutrymme, där rotmappen innehåller index.html och underkatalogerna a/ respektive b/ används för subdomänerna.

Workflowen i `.github/workflows/deploy.yml` använder nu SCP över SSH och förväntar sig ovan secrets. För att skapa `SSH_PRIVATE_KEY` i GitHub: kopiera innehållet i din privata nyckelfil (t.ex. `~/.ssh/id_rsa`) in i en ny Secret i repoinställningarna.

Om du ser "Welcome to nginx!" betyder det att domänen når servern men att servern fortfarande använder sin standardvhost. Då måste du lägga till en egen vhost för respektive domän. Exempelkoden finns i [nginx-vhost.conf](nginx-vhost.conf).
