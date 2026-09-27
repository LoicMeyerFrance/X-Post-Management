# Architecture technique

## Vue d'ensemble

L'application est composee de :

- **Backend Flask** (`server/app.py`) : API REST + sert le frontend compile
- **Bot Playwright** (`server/bot.py`) : automatisation de X via un navigateur Chrome
- **Scheduler APScheduler** (`server/scheduler.py`) : execute les posts programmes
- **Database SQLite** (`server/database.py`) : stockage des posts et historique followers dans `data/posts.db`
- **Frontend React** (`ui/`) : interface SPA avec Vite, TailwindCSS, TypeScript, Recharts

## Fichiers principaux

| Fichier | Role |
|---|---|
| `server/app.py` | Serveur Flask, routes API, sert `ui/dist/` en production |
| `server/bot.py` | Login X, publication, recuperation profil (bio, followers, badge) via Playwright |
| `server/scheduler.py` | Verifie les posts `scheduled` dont la date est passee et les publie |
| `server/database.py` | CRUD SQLite, tables `posts` et `followers_history` |
| `server/paths.py` | Chemins de fichiers (compatible PyInstaller) |
| `ui/src/App.tsx` | Composant racine, routing par pages |
| `ui/src/contexts/SettingsContext.tsx` | Etat global : langue, theme, verification config, preferences persistantes |
| `ui/src/lib/api.ts` | Client API (fetch vers Flask) |
| `ui/src/lib/i18n.ts` | Traductions FR/EN |

## Pages frontend

| Page | Fichier | Description |
|---|---|---|
| Composer | `ui/src/pages/Composer.tsx` | Creation et publication de posts |
| Planification | `ui/src/pages/Schedule.tsx` | Posts programmes |
| Calendrier | `ui/src/pages/Calendar.tsx` | Vue calendrier des publications |
| Historique | `ui/src/pages/History.tsx` | Posts publies et erreurs |
| Logs | `ui/src/pages/Logs.tsx` | Journal d'activite |
| Mon Profil | `ui/src/pages/Profile.tsx` | Stats du compte, graphique de croissance followers |
| Parametres | `ui/src/pages/Settings.tsx` | Configuration, connexion, import profil |
| A propos | `ui/src/pages/About.tsx` | FAQ et informations |

## API Endpoints

### Posts
| Methode | Route | Description |
|---|---|---|
| `GET` | `/api/posts` | Liste tous les posts (filtre `?status=`) |
| `POST` | `/api/posts` | Creer un post (FormData: text, image, status, scheduled_at) |
| `GET` | `/api/posts/:id` | Detail d'un post |
| `PUT` | `/api/posts/:id` | Modifier un post |
| `DELETE` | `/api/posts/:id` | Supprimer un post |
| `POST` | `/api/posts/:id/post-now` | Met en file et repond `202` ; suivre le statut du post |
| `POST` | `/api/posts/:id/schedule-now` | Met en file et repond `202` ; suivre le statut du post |
| `POST` | `/api/posts/:id/retry` | Retenter un post en erreur |
| `POST` | `/api/posts/:id/duplicate` | Dupliquer un post |
| `POST` | `/api/posts/:id/delete-from-x` | Supprimer un tweet publie de X |
| `POST` | `/api/posts/:id/delete-scheduled-from-x` | Supprimer un tweet programme de X |
| `POST` | `/api/posts/:id/remove-media` | Retirer le media d'un post |

### Profil
| Methode | Route | Description |
|---|---|---|
| `GET` | `/api/profile` | Profil local (nom, username, bio, followers, badge) |
| `POST` | `/api/profile/fetch` | Recuperer le profil depuis X + snapshot followers |
| `GET` | `/api/profile/stats` | Profil + historique complet des followers |
| `GET` | `/api/profile/picture` | Photo de profil |

### Configuration
| Methode | Route | Description |
|---|---|---|
| `GET` | `/api/settings/env` | Lire la configuration .env |
| `POST` | `/api/settings/env` | Sauvegarder la configuration .env |
| `GET` | `/api/settings/test-connection` | Tester la connexion a X |
| `POST` | `/api/settings/connect-x` | Se connecter a X dans une fenetre visible |
| `GET` | `/api/settings/connection-status` | Etat de la derniere connexion (sans navigateur) |
| `GET` | `/api/settings/preferences` | Lire les preferences UI (langue, theme) |
| `POST` | `/api/settings/preferences` | Sauvegarder les preferences UI |

### Autres
| Methode | Route | Description |
|---|---|---|
| `GET` | `/api/logs` | 200 dernieres lignes de logs (`?fingerprint=` pour eviter un renvoi inutile) |
| `GET` | `/api/health` | Etat du serveur + version de l'application |
| `GET` | `/api/detect-chrome` | Auto-detection Chrome sur le systeme |
| `GET` | `/uploads/:filename` | Fichiers uploades (images) |

## Base de donnees

Table `posts` :
```sql
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    scheduled_at TEXT,
    status TEXT DEFAULT 'draft',  -- draft|scheduled|scheduling|scheduled_on_x|posting|posted|error
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    posted_at TEXT,
    error_message TEXT DEFAULT '',
    retries_count INTEGER DEFAULT 0,
    tweet_url TEXT DEFAULT ''
)
```

Table `followers_history` :
```sql
CREATE TABLE followers_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL
)
```

## Flux de publication

1. L'utilisateur cree un post (status=`draft` ou `scheduled`)
2. Si `scheduled` : le scheduler verifie toutes les N secondes
3. Quand la date est passee : status -> `posting`, appel `bot.post_to_x()`
4. Succes : status -> `posted` + tweet_url / Echec : status -> `error` avec retry

## Flux de recuperation profil

1. L'utilisateur clique "Importer le profil" ou "Rafraichir"
2. `bot.fetch_profile()` : login X, navigation vers la page profil
3. Scraping : nom, bio, badge, photo, followers, following, date d'inscription
4. Sauvegarde : `profile_info.json`, `profile_picture.jpg`, snapshot dans `followers_history`

## Frontend

- **Build** : `cd ui && npm run build` -> genere `ui/dist/`
- **Dev** : `cd ui && npm run dev` -> Vite dev server sur :5173
- **i18n** : toutes les chaines dans `ui/src/lib/i18n.ts`
- **Preferences** : sauvegardees serveur-side dans `data/preferences.json` (persistent entre sessions pywebview)
- **Blocage** : si `X_USERNAME` ou `X_PASSWORD` vide, l'app force la page Parametres
