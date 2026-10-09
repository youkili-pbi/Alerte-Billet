# Alerte billets business Paris / Marseille → Asie (< 1 000 €)

Toutes les heures, le script vérifie un lot de 50 recherches sur Google Flights
(classe business, 1 adulte, 1 escale max) et met à jour la page web du dépôt
(`index.html`, publiée avec GitHub Pages) avec tous les billets sous 1 300 € :
en vert ceux sous 1 000 €. E-mail en option si les secrets SMTP sont renseignés. Le lot suivant reprend là où le précédent s'est arrêté : les ~800
combinaisons sont toutes vérifiées en ~16 heures.

- **Novembre–décembre 2026** : allers-retours (départ tous les 6 jours, séjour de 10 jours)
- **Janvier–juin 2027** : allers simples (une date tous les 20 jours)
- **Départs** : Paris-CDG et Marseille · **Arrivées** : 19 aéroports d'Asie

Tout se règle dans `config.yaml`.

## Mise en place
1. Créer un dépôt **public** sur GitHub (minutes d'exécution illimitées).
2. Y déposer `watcher.py`, `config.yaml`, `requirements.txt`, `README.md`, et créer
   le fichier `.github/workflows/watch.yml` (*Add file → Create new file*, taper le chemin complet).
3. *Settings → Secrets and variables → Actions* : ajouter `SMTP_HOST` (smtp.gmail.com),
   `SMTP_PORT` (465), `SMTP_USER`, `SMTP_PASSWORD` (mot de passe d'application Gmail), `ALERT_TO`.
4. *Actions → Surveillance prix business → Run workflow* pour un premier test.

## Bon à savoir
- `fast-flights` lit Google Flights sans API officielle : si Google bloque ou change sa page,
  le job échoue et GitHub t'envoie un e-mail. Seule la fonction `fetch_quote()` serait à remplacer.
- Test local sans réseau : `python watcher.py --fake`
