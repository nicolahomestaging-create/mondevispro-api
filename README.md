# MonDevisPro API

API de génération de devis PDF professionnels.

## 🚀 Déploiement rapide sur Railway

1. Crée un compte sur [Railway](https://railway.app)
2. Clique sur "New Project" → "Deploy from GitHub repo"
3. Connecte ton repo GitHub
4. Railway déploie automatiquement !

## 📡 Endpoints

### `GET /`
Vérifier que l'API fonctionne.

### `POST /generer-devis`
Génère un devis PDF.

**Body (JSON):**
```json
{
  "entreprise": {
    "nom": "Martin Rénovation",
    "gerant": "Pierre Martin",
    "siret": "123 456 789 00012",
    "adresse": "15 rue des Artisans",
    "cp_ville": "75011 Paris",
    "tel": "06 12 34 56 78",
    "email": "contact@martin-renovation.fr"
  },
  "client": {
    "nom": "Monsieur Dupont",
    "adresse": "42 avenue des Fleurs",
    "cp_ville": "75015 Paris",
    "tel": "06 98 76 54 32"
  },
  "prestations": [
    {
      "description": "Fourniture et pose fenêtre PVC",
      "quantite": 2,
      "unite": "unité",
      "prix_unitaire": 450
    }
  ],
  "tva_taux": 20,
  "conditions_paiement": "30% à la commande, solde à réception",
  "delai_realisation": "2 semaines"
}
```

**Réponse:**
```json
{
  "success": true,
  "numero_devis": "DEV-20241223-ABC123",
  "total_ttc": 1080.00,
  "pdf_filename": "DEV-20241223-ABC123.pdf",
  "pdf_url": "/download/DEV-20241223-ABC123.pdf"
}
```

### `GET /download/{filename}`
Télécharge le PDF généré.

## 🔧 Développement local

```bash
pip install -r requirements.txt
python main.py
```

L'API sera disponible sur `http://localhost:8000`

## 📝 Intégration Make.com

1. Ajoute un module **HTTP - Make a request**
2. URL: `https://ton-api.railway.app/generer-devis`
3. Method: `POST`
4. Body type: `JSON`
5. Mappe les variables depuis ton JSON Parse
