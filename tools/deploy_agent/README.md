# Agente di deploy ISEOPilot

Gira **sull'host**, non nel container. È l'unico componente che scrive sul
repository e tocca i container.

## Perché è separato dall'applicazione

Due motivi, entrambi concreti:

- **L'applicazione non può modificare se stessa.** Se una modifica rompe
  l'avvio, lo strumento per rimediare non è dentro ciò che è rotto: l'agente
  è ancora vivo e ha già riportato indietro la versione da solo.
- **Il container non ha accesso al socket Docker.** Montarlo equivarrebbe a
  dare root sull'host a chiunque sia amministratore di ISEOPilot — sullo
  stesso host girano Flusso-AI e iseotraining.

## Ambito, cablato nel codice

```
repository : /opt/iseopilot
servizio   : iseopilot (in docker-compose.prod.yml)
```

Non sono parametri: nessun comando costruisce il proprio bersaglio dai dati
della richiesta, non esiste `shell=True`, e gli argomenti sono sempre liste
esplicite. Una richiesta malformata o malevola non può spostare l'agente su
un altro percorso o su un altro container.

## Cosa fa una pubblicazione

1. rifiuta di partire se il repository sul server ha modifiche non committate
2. `git pull --ff-only`, poi applica la patch e committa
3. mette da parte l'immagine attuale come `iseopilot:precedente`
4. ricostruisce e riavvia **solo** il servizio `iseopilot`
5. interroga `/healthz` fino a 90 secondi
6. **se risponde**: `git push` su `main`
7. **se non risponde**: torna al codice e all'immagine precedenti, riavvia e
   verifica di nuovo. Il push non avviene: `main` resta pulito.

Il punto 3 è ciò che rende il ritorno indietro veloce — non serve
ricostruire nulla.

## Installazione

```bash
sudo mkdir -p /etc/iseopilot
openssl rand -hex 32 | sudo tee /etc/iseopilot/deploy-agent.token
sudo chmod 600 /etc/iseopilot/deploy-agent.token

sudo cp /opt/iseopilot/tools/deploy_agent/iseopilot-deploy-agent.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iseopilot-deploy-agent
sudo systemctl status iseopilot-deploy-agent
```

Lo stesso token va poi messo nell'applicazione (pagina **Motore**), insieme
all'indirizzo `unix:/run/iseopilot/deploy-agent.sock`.

## Perché un socket unix e non una porta

L'applicazione gira in un **container**: lì dentro `127.0.0.1` è il container
stesso, non l'host. Una porta andrebbe quindi esposta su un'interfaccia
raggiungibile dai container — cioè anche dalla rete aziendale.

Il socket attraversa quel confine come un file montato
(`- /run/iseopilot:/run/iseopilot` nel compose): **nessuna porta aperta su
nessuna interfaccia**, e il controllo d'accesso sono i permessi del file.
Il socket è `0660 root:10001`, dove 10001 è l'uid/gid di `appuser` dentro il
container: nessun altro utente dell'host può contattarlo.

Se nel container l'utente cambia, va aggiornato `DEPLOY_AGENT_GID` nell'unità
systemd.

## Verifica

```bash
sudo systemctl status iseopilot-deploy-agent --no-pager
ls -l /run/iseopilot/deploy-agent.sock
sudo tail -f /var/log/iseopilot-deploy.log
```

Dal container, per controllare che il socket sia visibile:

```bash
sudo docker exec iseopilot ls -l /run/iseopilot/deploy-agent.sock
```
