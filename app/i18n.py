"""
i18n.py — Internazionalizzazione IT/EN dell'interfaccia web.

Il motore desktop (i18n.py originale) cammina l'albero dei widget Qt: non
riusabile per il web. Qui la traduzione avviene a tempo di rendering Jinja.

Principio (identico alla regola di prodotto desktop):
  * L'interfaccia esiste in italiano e inglese.
  * È in italiano SOLO quando la lingua interfaccia è "it"; per ogni altro
    valore è in inglese.
  * La lingua di RISPOSTA dell'AI è indipendente (resta il selettore in chat).

Uso nei template:  {{ t("Accesso sicuro") }}
  * La CHIAVE è il testo italiano (così il fallback è sempre sensato).
  * Se la traduzione inglese manca, t() ritorna l'italiano invariato: non
    rompe mai il rendering, e la copertura si può ampliare in modo incrementale.
"""
from __future__ import annotations


def normalize(lang: str) -> str:
    """Qualsiasi valore diverso da 'it' diventa 'en'."""
    return "it" if (lang or "").lower().startswith("it") else "en"


# Tabella IT -> EN. Solo le stringhe che DIFFERISCONO tra le due lingue.
# La chiave è il letterale italiano usato nei template.
PAIRS = {
    # ── Navigazione / topbar ──
    "Chat": "Chat",
    "Conoscenza": "Knowledge",
    "Connessioni": "Connections",
    "Motore": "Engine",
    "Utenti": "Users",
    "Dipartimenti": "Departments",
    "Area": "Area",
    "Esci": "Sign out",
    "Lingua": "Language",

    # ── Login ──
    "Accesso sicuro": "Secure access",
    "Inserisci le credenziali fornite dall'amministratore.":
        "Enter the credentials provided by your administrator.",
    "Nome utente": "Username",
    "Password": "Password",
    "Sblocca accesso": "Unlock access",
    "Sblocco in corso…": "Unlocking…",
    "Conoscenza aziendale, protetta per area.":
        "Company knowledge, secured per area.",
    "Assistente AI interno con motore Claude e modello locale. Ogni dipartimento ha la propria base di conoscenza isolata. Dati sensibili anonimizzati prima di lasciare il perimetro.":
        "Internal AI assistant with Claude engine and local model. Each department has its own isolated knowledge base. Sensitive data is anonymized before leaving the perimeter.",
    "Accesso gestito": "Managed access",
    "Segreti cifrati": "Encrypted secrets",
    "Anonimizzazione": "Anonymization",
    "ISEO Serrature S.p.A. · Piattaforma interna · L'accesso è registrato.":
        "ISEO Serrature S.p.A. · Internal platform · Access is logged.",

    # ── Chat ──
    "Motore AI": "AI engine",
    "Tono": "Tone",
    "Lingua risposta": "Reply language",
    "Scrivi un messaggio…": "Type a message…",
    "Invia": "Send",
    "L'assistente può commettere errori. Verifica le informazioni importanti.":
        "The assistant can make mistakes. Verify important information.",

    # ── Conoscenza ──
    "La conoscenza del mio dipartimento": "My department's knowledge",
    "Carica documenti": "Upload documents",
    "Aggiungi alla conoscenza del dipartimento": "Add to department knowledge",
    "Trascina qui i file o": "Drag files here or",
    "scegli dal computer": "choose from your computer",
    "Carica e indicizza": "Upload and index",
    "Nella collezione del dipartimento": "In the department collection",
    "Documento": "Document",
    "Azione": "Action",
    "Rimuovi": "Remove",
    "Cartelle locali o di rete": "Local or network folders",
    "Aggiorna indice cartelle": "Refresh folder index",
    "passaggi indicizzati in totale": "indexed passages in total",

    # ── Connessioni (toggle) ──
    "Cosa può consultare l'assistente": "What the assistant can consult",
    "Conoscenza interna": "Internal knowledge",
    "Conoscenza del dipartimento (ChromaDB)": "Department knowledge (ChromaDB)",
    "Cartelle del dipartimento": "Department folders",
    "Gestisci": "Manage",
    "Connettori personali Microsoft": "Personal Microsoft connectors",
    "Salva preferenze": "Save preferences",
    "Preferenze salvate.": "Preferences saved.",
    "connesso": "connected",
    "non connesso": "not connected",
    "Connetti il mio account": "Connect my account",
    "Disconnetti": "Disconnect",

    # ── Admin: motore ──
    "Motore, anonimizzazione & conoscenza": "Engine, anonymization & knowledge",
    "Credenziali e modelli": "Credentials and models",
    "Connessione AI": "AI connection",
    "Privacy": "Privacy",
    "Salva tutte le impostazioni": "Save all settings",
    "Impostazioni salvate.": "Settings saved.",
    "Configurazione (auto-compilata con i valori ISEO)":
        "Configuration (auto-filled with ISEO values)",
    "Auto-compila con i valori ISEO": "Auto-fill with ISEO values",

    # ── Admin: dipartimenti / browser ──
    "Crea dipartimento": "Create department",
    "Aree esistenti": "Existing areas",
    "Cartelle ricercabili": "Searchable folders",
    "Cartelle": "Folders",
    "Sfoglia / aggiungi": "Browse / add",
    "Vai a una cartella": "Go to a folder",
    "Apri": "Open",
    "cartella superiore": "parent folder",
    "Torna ai dipartimenti": "Back to departments",

    # ── Stato / generici ──
    "non installato": "not installed",
    "disponibile": "available",
    "impostata": "set",
    "mancante": "missing",
    "impostato": "set",
    "nessuna": "none",
    "Salva": "Save",

    # ── Chat (statusbar + composer + bolla) ──
    "Stato": "Status",
    "Pronto": "Ready",
    "attiva": "on",
    "Area conoscenza": "Knowledge area",
    "Sistema": "System",
    "locale": "local",
    "Assistente AI ISEO. Scrivi una domanda: la risposta arriva in streaming dal motore selezionato. Con Claude i dati tecnici sensibili (IP, email, codici fiscali, P.IVA, hash, token…) vengono anonimizzati prima dell'invio.":
        "ISEO AI assistant. Type a question: the answer streams from the selected engine. With Claude, sensitive technical data (IP, email, tax codes, VAT numbers, hashes, tokens…) is anonymized before sending.",
    "Scrivi la tua domanda…  (Invio per inviare · Maiusc+Invio per andare a capo)":
        "Type your question…  (Enter to send · Shift+Enter for new line)",

    # ── Eyebrow / intro pagine ──
    "Base di conoscenza · area": "Knowledge base · area",
    "Tutto ciò che carichi qui finisce nella collezione del tuo dipartimento": 
        "Everything you upload here goes into your department's collection",
    "Fonti nelle risposte": "Sources in answers",
    "Accendi o spegni le fonti che l'assistente usa per arricchire le risposte. Le scelte valgono solo per te. Conoscenza e cartelle sono limitate al tuo dipartimento":
        "Turn on or off the sources the assistant uses to enrich answers. Choices apply only to you. Knowledge and folders are limited to your department",
    "Amministrazione · impostazioni globali": "Administration · global settings",
    "Configurazione valida per tutti gli utenti. I segreti sono cifrati a riposo; le chiavi non vengono mai mostrate. L'utente sceglie poi quale motore usare in chat.":
        "Settings apply to all users. Secrets are encrypted at rest; keys are never shown. The user then chooses which engine to use in chat.",
    "Amministrazione · aree di conoscenza": "Administration · knowledge areas",
    "Amministrazione · cartelle ricercabili del server": "Administration · searchable server folders",
    "Naviga il filesystem del server (incluse le share di rete montate) e aggiungi le cartelle che gli utenti del dipartimento potranno cercare. I documenti restano dove sono: vengono indicizzati sul posto.":
        "Browse the server filesystem (including mounted network shares) and add the folders that the department's users can search. Documents stay where they are: they are indexed in place.",
    "Aggiungi documenti": "Add documents",
    "Crea dipartimento / area": "Create department / area",
    "Seleziona cartelle": "Select folders",
    "Stai esplorando": "You are browsing",
    "Già assegnate a": "Already assigned to",
    "Percorso": "Path",
    "indicizzati": "indexed",
    "configurate": "configured",
    "Modalità": "Mode",
    "Documentale": "Document-based",
    "Account": "Account",
    "Il tuo account": "Your account",
    "Gestisci la password del tuo accesso. La password è cifrata e non è visibile a nessuno, nemmeno all'amministratore.": "Manage your sign-in password. The password is hashed and not visible to anyone, not even the administrator.",
    "Password aggiornata.": "Password updated.",
    "La password attuale non è corretta.": "The current password is not correct.",
    "La nuova password deve avere almeno 8 caratteri.": "The new password must be at least 8 characters.",
    "La nuova password e la conferma non coincidono.": "The new password and confirmation do not match.",
    "La nuova password deve essere diversa da quella attuale.": "The new password must differ from the current one.",
    "Sicurezza": "Security",
    "Cambia password": "Change password",
    "Password attuale": "Current password",
    "Nuova password": "New password",
    "Almeno 8 caratteri.": "At least 8 characters.",
    "Conferma nuova password": "Confirm new password",
    "Aggiorna password": "Update password",
    "Audit": "Audit",
    "Registro attività": "Activity log",
    "Audit trail": "Audit trail",
    "Registro delle attività degli utenti: accessi, ricerche, generazione documenti, connessioni, modifiche. Sono tracciati azione, utente, orario (UTC) e IP — mai il contenuto delle conversazioni.": "Log of user activity: logins, searches, document generation, connections, changes. Action, user, time (UTC) and IP are tracked — never the content of conversations.",
    "Filtri": "Filters",
    "Periodo": "Period",
    "Ultime 24 ore": "Last 24 hours",
    "Ultimi 7 giorni": "Last 7 days",
    "Ultimi 30 giorni": "Last 30 days",
    "Intervallo personalizzato": "Custom range",
    "Tutto": "All",
    "Dal": "From",
    "Al": "To",
    "Tutti": "All",
    "Tutte": "All",
    "Applica": "Apply",
    "eventi": "events",
    "Esporta in Excel": "Export to Excel",
    "Data/ora (UTC)": "Date/time (UTC)",
    "Dettaglio": "Detail",
    "Nessun evento nel periodo selezionato.": "No events in the selected period.",
    "risposta da un apparato di rete (proxy/VPN), non da ISEOPilot: la richiesta è stata interrotta prima di arrivare. Riprova; se persiste avvisa l'amministratore.": "response from a network appliance (proxy/VPN), not from ISEOPilot: the request was cut off before arriving. Retry; if it persists contact the administrator.",
    "Disconnettere questo account?": "Disconnect this account?",
    "Dove cercare?": "Where to search?",
    "Conoscenza": "Knowledge",
    "Cartelle": "Folders",
    "Seleziona una fonte dati (Conoscenza, Cartelle, OneDrive o Dynamics 365) prima di inviare, oppure passa alla modalità AI libera.": "Select a data source (Knowledge, Folders, OneDrive or Dynamics 365) before sending, or switch to Free AI mode.",
    "Nessuna cartella configurata per il tuo reparto: chiedi all'amministratore.": "No folder configured for your department: ask the administrator.",
    "OneDrive non è connesso: collegalo dalla pagina Connessioni.": "OneDrive is not connected: link it from the Connections page.",
    "Dynamics 365 non è connesso: collegalo dalla pagina Connessioni.": "Dynamics 365 is not connected: link it from the Connections page.",
    "Power BI non è connesso: collegalo dalla pagina Connessioni.": "Power BI is not connected: link it from the Connections page.",

    # ── Connettore Power BI ──
    "Connettore Power BI": "Power BI connector",
    "Interroga i modelli semantici Power BI con la tua identità: valgono i tuoi permessi (Lettura + Build sul dataset) e la tua Row-Level Security.":
        "Query Power BI semantic models with your identity: your permissions (Read + Build on the dataset) and your Row-Level Security apply.",
    "Catalogo": "Catalog",
    "interrogabili": "queryable",
    "Catalogo non ancora generato": "Catalog not generated yet",
    "Genera catalogo": "Generate catalog",
    "Rigenera catalogo": "Regenerate catalog",
    "Catalogo generato": "Catalog generated",
    "Avvio generazione catalogo…": "Starting catalog generation…",
    "Errore di rete.": "Network error.",
    "Il catalogo elenca i workspace e i dataset visibili alla TUA utenza (tabelle, colonne e, dove disponibili, misure): è ciò che permette all'assistente di costruire query DAX affidabili. Rigeneralo quando cambiano i tuoi permessi o i modelli.":
        "The catalog lists the workspaces and datasets visible to YOUR account (tables, columns and, where available, measures): it is what lets the assistant build reliable DAX queries. Regenerate it when your permissions or the models change.",

    # ── Modifica documenti allegati ──
    "Sto modificando il documento…": "Editing the document…",

    # ── Memoria personale (note) ──
    "Memoria personale": "Personal memory",
    "Note che l'assistente ricorda per te": "Notes the assistant remembers for you",
    "Preferenze e correzioni durevoli tra le chat (es. «le specifiche tecniche le voglio in spagnolo»). Si aggiungono qui o scrivendo in chat «ricordati che…». Sono visibili solo a te, cifrate sul server e cancellabili in ogni momento; una richiesta esplicita nel messaggio corrente prevale comunque.":
        "Durable preferences and corrections across chats (e.g. \u00abI want technical specifications in Spanish\u00bb). Add them here or by writing \u00abremember that\u2026\u00bb in chat. They are visible only to you, encrypted on the server and deletable at any time; an explicit request in the current message always prevails.",
    "Nessuna nota salvata.": "No saved notes.",
    "Es.: rispondimi sempre in spagnolo": "E.g.: always reply to me in Spanish",
    "Aggiungi": "Add",
    "Svuota tutto": "Clear all",
    "Eliminare tutte le note personali?": "Delete all personal notes?",

    # ── Dub Studio ──
    "Doppiaggio e sottotitoli dei video": "Video dubbing and subtitles",
    "Due modalità: «Doppiaggio» sostituisce l'audio con la TUA voce clonata nella lingua scelta (serve il profilo voce registrato con consenso); «Sottotitoli» mantiene l'audio originale e imprime i sottotitoli tradotti nel video. In entrambe: trascrizione, traduzione e una revisione dei testi PRIMA di produrre il file.":
        "Two modes: \u00abDubbing\u00bb replaces the audio with YOUR cloned voice in the chosen language (requires the voice profile recorded with consent); \u00abSubtitles\u00bb keeps the original audio and burns the translated subtitles into the video. In both: transcription, translation and a text review BEFORE producing the file.",
    "Il worker di elaborazione non risulta attivo: i job in coda non partiranno finché non viene avviato. Segnala all'amministratore.":
        "The processing worker is not running: queued jobs will not start until it is up. Report it to the administrator.",
    "Profilo voce": "Voice profile",
    "La tua voce per il doppiaggio": "Your voice for dubbing",
    "Registra 60–90 secondi leggendo il testo qui sotto. La voce usata nel doppiaggio è SOLO questa: registrata da te, con consenso esplicito, visibile e cancellabile in ogni momento. Mai clonata dall'audio dei video.":
        "Record 60\u201390 seconds reading the text below. The voice used for dubbing is ONLY this one: recorded by you, with explicit consent, visible and deletable at any time. Never cloned from the videos' audio.",
    "Avvia registrazione": "Start recording",
    "Ferma": "Stop",
    "Acconsento all'uso di questa registrazione della mia voce per generare i doppiaggi dei MIEI video in Dub Studio.":
        "I consent to the use of this recording of my voice to generate the dubbing of MY videos in Dub Studio.",
    "Salva profilo voce": "Save voice profile",
    "Riprova": "Retry",
    "Profilo voce presente": "Voice profile saved",
    "Elimina il mio profilo voce": "Delete my voice profile",
    "Eliminare definitivamente il tuo profilo voce?": "Permanently delete your voice profile?",
    "Il consenso esplicito è obbligatorio per salvare la voce.": "Explicit consent is required to save your voice.",
    "Microfono non disponibile o permesso negato.": "Microphone unavailable or permission denied.",
    "Nuovo lavoro": "New job",
    "Carica un video": "Upload a video",
    "Sorgente": "Source",
    "Destinazione": "Target",
    "Sottotitoli (audio originale + sottotitoli impressi)": "Subtitles (original audio + burned-in subtitles)",
    "Doppiaggio (con la mia voce clonata)": "Dubbing (with my cloned voice)",
    "Avvia": "Start",
    "Limiti": "Limits",
    "minuti": "minutes",
    "Caricamento…": "Uploading…",
    "Video caricato: elaborazione avviata.": "Video uploaded: processing started.",
    "Seleziona un file video.": "Select a video file.",
    "Lavori": "Jobs",
    "I tuoi job": "Your jobs",
    "Nessun lavoro ancora.": "No jobs yet.",
    "in coda (trascrizione)": "queued (transcription)",
    "trascrizione in corso": "transcribing",
    "in coda (traduzione)": "queued (translation)",
    "traduzione in corso": "translating",
    "in attesa della tua revisione": "waiting for your review",
    "in coda (montaggio sottotitoli)": "queued (subtitle rendering)",
    "in coda (sintesi vocale)": "queued (voice synthesis)",
    "montaggio in corso": "rendering",
    "sintesi vocale in corso (su CPU richiede tempo)": "voice synthesis in progress (CPU: this takes time)",
    "pronto": "ready",
    "errore": "error",
    "Rivedi le traduzioni": "Review translations",
    "Scarica video": "Download video",
    "Scarica .srt": "Download .srt",
    "Elimina": "Delete",
    "Eliminare questo lavoro e i suoi file?": "Delete this job and its files?",
    "Revisione": "Review",
    "Controlla e correggi le traduzioni": "Check and fix the translations",
    "Il testo verrà letto (o impresso) esattamente come lo confermi. Il budget indica i caratteri che stanno nei tempi del parlato: se lo superi, il doppiaggio accelera o sborda.":
        "The text will be spoken (or burned in) exactly as you confirm it. The budget shows how many characters fit the speech timing: exceed it and the dubbing speeds up or overflows.",
    "budget": "budget",
    "caratteri": "characters",
    "Salva le modifiche": "Save changes",
    "Scarica sottotitoli (.srt)": "Download subtitles (.srt)",
    "Conferma e produci il video": "Confirm and produce the video",
    "Modifiche salvate.": "Changes saved.",

    # ── Template documento personale ──
    "Template personale (.docx/.dotx/.pptx/.potx): se caricato, i documenti generati usano il TUO template al posto di quello ISEO. Niente macro.":
        "Personal template (.docx/.dotx/.pptx/.potx): when loaded, generated documents use YOUR template instead of the ISEO one. No macros.",
    "Template": "Template",
    "Rimuovi template": "Remove template",
    "Ricerca in corso su": "Search in progress on",
    "l'operazione può richiedere qualche istante.": "this may take a few moments.",
    "Sto preparando il file…": "Preparing the file…",
    "La password non rispetta i requisiti: almeno 12 caratteri, una lettera maiuscola e un carattere speciale.": "The password does not meet the requirements: at least 12 characters, one uppercase letter and one special character.",
    "Almeno 12 caratteri, con almeno una lettera maiuscola e un carattere speciale (es. ! ? @ # - _).": "At least 12 characters, with at least one uppercase letter and one special character (e.g. ! ? @ # - _).",
    "Caricamento": "Uploading",
    "Seleziona almeno un file (o trascina una cartella).": "Select at least one file (or drag a folder).",
    "se persiste, ricarica la pagina e ripeti l'accesso": "if it persists, reload the page and sign in again",
    "blocchi": "chunks",
    "Nota: le immagini allegate non passano dall'anonimizzazione e vengono inviate a Claude così come sono.": "Note: attached images do not go through anonymisation and are sent to Claude as they are.",
    "Interfaccia non aggiornata: clicca qui per ricaricare la versione corrente.": "Interface out of date: click here to reload the current version.",
    "AI libera": "Free AI",
    "Attività": "Tasks",
    "Allega file (fino a 20)": "Attach files (up to 20)",
    "Estraggo il testo…": "Extracting text…",
    "Non leggibile": "Not readable",
    "Massimo 20 allegati": "Maximum 20 attachments",
    "Fonti": "Sources",
    "Le modifiche agli interruttori si salvano da sole.": "Switch changes are saved automatically.",
    "Salvato": "Saved",
    "Nuova chat": "New chat",
    "Conversazioni": "Conversations",
    "Nessuna conversazione salvata": "No saved conversations",
    "Risposta utile — salva come esempio": "Helpful — save as example",
    "Risposta non utile": "Not helpful",
    "Salvata come esempio": "Saved as example",
    "Segnalazione registrata": "Feedback recorded",
    "Eliminare questa conversazione?": "Delete this conversation?",
    "Nuovo titolo della conversazione:": "New conversation title:",
    "Tu": "You",
    "Assistente": "Assistant",
    "Attendere…": "Please wait…",
    "Operazione in corso, non chiudere la pagina.": "Operation in progress, do not close the page.",
    "Carico la base di conoscenza…": "Loading the knowledge base…",
    "Carico e indicizzo i documenti…": "Uploading and indexing documents…",
    "Aggiorno l'indice delle cartelle…": "Refreshing the folder index…",
    "Aggiungo e indicizzo la cartella…": "Adding and indexing the folder…",
    "Genero il catalogo entità… può richiedere qualche minuto": "Generating the entity catalog… this may take a few minutes",
    "Documentale: cerca nelle fonti. AI libera: conoscenza generale, senza cercare nei documenti.":
        "Document-based: searches your sources. Free AI: general knowledge, without searching the documents.",
}


def t(key: str, lang: str = "it") -> str:
    """Traduce 'key' (testo italiano) nella lingua data. Fallback: italiano."""
    if normalize(lang) == "it":
        return key
    return PAIRS.get(key, key)
