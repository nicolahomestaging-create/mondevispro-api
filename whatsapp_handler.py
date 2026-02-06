"""
Vocario WhatsApp Handler v8 - State Machine
Module séparé avec APIRouter - s'intègre dans main.py via setup()

Features:
- State machine (pas d'IA pour le flow, seulement pour parser les prestations)
- Suppression devis/factures via WhatsApp (sync dashboard)
- Meilleur affichage documents avec statuts et factures groupées
- Changement contact à l'envoi
- Toujours un message de fin avec hint "Tapez menu"
- Fix lien signature (UUID Supabase)
- Retour arrière à chaque étape
"""

import os
import json
import uuid
import re
import logging
import traceback
import requests
import resend
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Form

logger = logging.getLogger("vocario.whatsapp")

# =============================================================================
# ROUTER FastAPI
# =============================================================================
router = APIRouter()

# =============================================================================
# DÉPENDANCES (injectées depuis main.py via setup())
# =============================================================================

# Clients
supabase_client = None
anthropic_client = None
openai_whisper_client = None

# Fonctions from main.py
get_entreprise_by_whatsapp = None
save_devis_to_dashboard = None
save_facture_to_dashboard = None
generer_pdf_devis = None
generer_word_devis = None
generer_pdf_facture = None
generer_word_facture = None
upload_to_supabase = None

# Models from main.py
Prestation = None
Entreprise = None
Client = None
DevisRequest = None
FactureRequest = None


def setup(deps: Dict[str, Any]):
    """
    Injecte les dépendances depuis main.py.
    Appelé UNE SEULE FOIS au démarrage.
    
    Usage dans main.py:
        from whatsapp_handler import router, setup
        setup({
            "supabase_client": supabase_client,
            "anthropic_client": anthropic_client,
            "openai_whisper_client": openai_whisper_client,
            "get_entreprise_by_whatsapp": get_entreprise_by_whatsapp,
            "save_devis_to_dashboard": save_devis_to_dashboard,
            "save_facture_to_dashboard": save_facture_to_dashboard,
            "generer_pdf_devis": generer_pdf_devis,
            "generer_word_devis": generer_word_devis,
            "generer_pdf_facture": generer_pdf_facture,
            "generer_word_facture": generer_word_facture,
            "upload_to_supabase": upload_to_supabase,
            "Prestation": Prestation,
            "Entreprise": Entreprise,
            "Client": Client,
            "DevisRequest": DevisRequest,
            "FactureRequest": FactureRequest,
        })
        app.include_router(router)
    """
    global supabase_client, anthropic_client, openai_whisper_client
    global get_entreprise_by_whatsapp, save_devis_to_dashboard, save_facture_to_dashboard
    global generer_pdf_devis, generer_word_devis, generer_pdf_facture, generer_word_facture
    global upload_to_supabase
    global Prestation, Entreprise, Client, DevisRequest, FactureRequest
    
    supabase_client = deps["supabase_client"]
    anthropic_client = deps["anthropic_client"]
    openai_whisper_client = deps.get("openai_whisper_client")
    get_entreprise_by_whatsapp = deps["get_entreprise_by_whatsapp"]
    save_devis_to_dashboard = deps["save_devis_to_dashboard"]
    save_facture_to_dashboard = deps["save_facture_to_dashboard"]
    generer_pdf_devis = deps["generer_pdf_devis"]
    generer_word_devis = deps["generer_word_devis"]
    generer_pdf_facture = deps["generer_pdf_facture"]
    generer_word_facture = deps["generer_word_facture"]
    upload_to_supabase = deps["upload_to_supabase"]
    Prestation = deps["Prestation"]
    Entreprise = deps["Entreprise"]
    Client = deps["Client"]
    DevisRequest = deps["DevisRequest"]
    FactureRequest = deps["FactureRequest"]
    
    logger.info("✅ WhatsApp handler setup complete")


# =============================================================================
# CONFIG TWILIO + RESEND
# =============================================================================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "+33759714586")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY
    logger.info("Resend configuré")

TEMPLATE_MENU_SID = os.getenv("TWILIO_TEMPLATE_MENU_SID", "HX66922d777c512200cad1d2622199645f")


# =============================================================================
# ÉTATS DE CONVERSATION
# =============================================================================

class State:
    MENU = "menu"
    # Devis
    DEVIS_NOM = "devis_nom"
    DEVIS_TEL = "devis_tel"
    DEVIS_EMAIL = "devis_email"
    DEVIS_ADRESSE = "devis_adresse"
    DEVIS_PROJET = "devis_projet"
    DEVIS_PRESTATIONS = "devis_prestations"
    DEVIS_PRESTATIONS_SUITE = "devis_prestations_suite"
    DEVIS_OPTIONS = "devis_options"
    DEVIS_REMISE = "devis_remise"
    DEVIS_ACOMPTE = "devis_acompte"
    DEVIS_DELAI = "devis_delai"
    DEVIS_RECAP = "devis_recap"
    DEVIS_MODIFIER = "devis_modifier"
    DEVIS_GENERE = "devis_genere"
    # Facture
    FACTURE_LISTE = "facture_liste"
    FACTURE_TYPE = "facture_type"
    FACTURE_ACOMPTE_TAUX = "facture_acompte_taux"
    FACTURE_GENERE = "facture_genere"
    # Documents
    DOCS_LISTE = "docs_liste"
    DOCS_DETAIL = "docs_detail"
    DOCS_ENVOYER_WA = "docs_envoyer_wa"
    DOCS_ENVOYER_EMAIL = "docs_envoyer_email"
    DOCS_SIGNATURE_CHOIX = "docs_signature_choix"
    DOCS_CONFIRMER_SUPPR = "docs_confirmer_suppr"


# =============================================================================
# CACHE CONVERSATIONS (Supabase + RAM)
# =============================================================================

_conversations: Dict[str, Dict] = {}
_processed_sids: Dict[str, datetime] = {}


def normalize_phone(phone: str) -> str:
    """Normalise un numéro: whatsapp:+33xxx -> 33xxx"""
    return phone.replace("whatsapp:", "").replace("+", "").strip()


def get_conv(phone: str) -> Dict:
    """Récupère la conversation (cache local → Supabase → nouvelle)"""
    phone = normalize_phone(phone)
    if phone in _conversations:
        return _conversations[phone]
    
    try:
        if supabase_client:
            result = supabase_client.table("whatsapp_conversations").select("*").eq("phone", phone).execute()
            if result.data and len(result.data) > 0:
                row = result.data[0]
                conv = {
                    "state": row.get("state", State.MENU),
                    "data": row.get("data", {}),
                    "last_activity": row.get("last_activity", datetime.now().isoformat()),
                }
                _conversations[phone] = conv
                return conv
    except Exception as e:
        logger.error(f"Erreur lecture conversation: {e}")
    
    conv = {"state": State.MENU, "data": {}, "last_activity": datetime.now().isoformat()}
    _conversations[phone] = conv
    return conv


def save_conv(phone: str, conv: Dict):
    """Sauvegarde dans cache + Supabase"""
    phone = normalize_phone(phone)
    conv["last_activity"] = datetime.now().isoformat()
    _conversations[phone] = conv
    
    try:
        if supabase_client:
            supabase_client.table("whatsapp_conversations").upsert({
                "phone": phone,
                "state": conv.get("state", State.MENU),
                "data": conv.get("data", {}),
                "last_activity": conv["last_activity"],
                "updated_at": datetime.now().isoformat(),
            }, on_conflict="phone").execute()
    except Exception as e:
        logger.error(f"Erreur sauvegarde conversation: {e}")


def reset_conv(phone: str):
    """Réinitialise la conversation"""
    phone = normalize_phone(phone)
    _conversations.pop(phone, None)
    try:
        if supabase_client:
            supabase_client.table("whatsapp_conversations").delete().eq("phone", phone).execute()
    except Exception as e:
        logger.error(f"Erreur reset conversation: {e}")


# =============================================================================
# FONCTIONS TWILIO
# =============================================================================

def send_whatsapp(to: str, body: str):
    """Envoie un message WhatsApp via Twilio"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning(f"Twilio non configuré, message non envoyé: {body[:50]}")
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        # S'assurer du format whatsapp:+xxx
        if not to.startswith("whatsapp:"):
            if not to.startswith("+"):
                to = f"+{to}"
            to = f"whatsapp:{to}"
        
        resp = requests.post(url, data={
            "From": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            "To": to,
            "Body": body,
        }, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        
        if resp.status_code in [200, 201]:
            logger.info(f"Message envoyé à {to}: {body[:50]}...")
            return True
        else:
            logger.error(f"Erreur Twilio {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Erreur envoi WhatsApp: {e}")
        return False


def send_whatsapp_template(to: str, template_sid: str):
    """Envoie un template WhatsApp (menu avec boutons)"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        # Fallback: envoyer le menu en texte
        send_whatsapp(to, "👋 *Bienvenue sur Vocario !*\n\nTapez:\n*1* → 📝 Nouveau devis\n*2* → 🧾 Nouvelle facture\n*3* → 📂 Mes documents")
        return True
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        if not to.startswith("whatsapp:"):
            if not to.startswith("+"):
                to = f"+{to}"
            to = f"whatsapp:{to}"
        
        resp = requests.post(url, data={
            "From": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            "To": to,
            "ContentSid": template_sid,
        }, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        
        if resp.status_code in [200, 201]:
            return True
        else:
            logger.error(f"Erreur template Twilio {resp.status_code}: {resp.text[:200]}")
            # Fallback texte
            send_whatsapp(to, "👋 *Bienvenue sur Vocario !*\n\nTapez:\n*1* → 📝 Nouveau devis\n*2* → 🧾 Nouvelle facture\n*3* → 📂 Mes documents")
            return True
    except Exception as e:
        logger.error(f"Erreur template: {e}")
        return False


def send_whatsapp_document(to: str, pdf_url: str, caption: str = ""):
    """Envoie un PDF via WhatsApp"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        if not to.startswith("whatsapp:"):
            if not to.startswith("+"):
                to = f"+{to}"
            to = f"whatsapp:{to}"
        
        data = {
            "From": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            "To": to,
            "MediaUrl": pdf_url,
        }
        if caption:
            data["Body"] = caption
        
        resp = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        return resp.status_code in [200, 201]
    except Exception as e:
        logger.error(f"Erreur envoi document: {e}")
        return False


# =============================================================================
# FONCTIONS EMAIL (Resend)
# =============================================================================

def send_email_devis(to_email: str, entreprise: Dict, devis: Dict, avec_signature: bool = False):
    """Envoie un devis par email avec template pro"""
    if not RESEND_API_KEY:
        logger.error("Resend non configuré")
        return False
    
    nom_entreprise = entreprise.get("nom", "")
    couleur = entreprise.get("couleur_pdf", "#2F665B")
    numero = devis.get("numero_devis", "")
    client_nom = devis.get("client_nom", "")
    total_ttc = devis.get("total_ttc", 0)
    pdf_url = devis.get("pdf_url", "")
    titre_projet = devis.get("titre_projet", "")
    
    # Construire le lien de signature si demandé
    signature_html = ""
    if avec_signature:
        devis_uuid = devis.get("id", "")
        if devis_uuid:
            signature_url = f"https://www.vocario.fr/signer/{devis_uuid}"
            signature_html = f'''
            <div style="text-align:center; margin:20px 0;">
                <a href="{signature_url}" style="background-color:{couleur}; color:white; padding:15px 30px; text-decoration:none; border-radius:8px; font-size:16px; font-weight:bold;">
                    ✍️ Signer le devis
                </a>
            </div>
            '''
    
    # Template email
    html = f'''
    <div style="max-width:600px; margin:0 auto; font-family:Arial,sans-serif;">
        <div style="background-color:{couleur}; padding:20px; text-align:center;">
            <h1 style="color:white; margin:0;">{nom_entreprise}</h1>
        </div>
        <div style="padding:30px; background:#f9f9f9;">
            <p>Bonjour {client_nom},</p>
            <p>Veuillez trouver ci-joint votre devis <strong>{numero}</strong>{f" pour le projet <em>{titre_projet}</em>" if titre_projet else ""}.</p>
            <div style="background:white; padding:15px; border-radius:8px; text-align:center; margin:20px 0;">
                <p style="color:#666; margin:0;">Montant Total TTC</p>
                <p style="font-size:28px; font-weight:bold; color:{couleur}; margin:5px 0;">{total_ttc:.2f} €</p>
            </div>
            {signature_html}
            <p>N'hésitez pas à nous contacter pour toute question.</p>
            <p>Cordialement,<br/><strong>{nom_entreprise}</strong></p>
            {f'<p>📞 {entreprise.get("tel", "")}</p>' if entreprise.get("tel") else ""}
        </div>
        <div style="text-align:center; padding:10px; color:#999; font-size:12px;">
            Envoyé via Vocario
        </div>
    </div>
    '''
    
    try:
        # Télécharger le PDF pour pièce jointe
        attachments = []
        if pdf_url and pdf_url.startswith("http"):
            try:
                pdf_resp = requests.get(pdf_url, timeout=15)
                if pdf_resp.status_code == 200:
                    import base64
                    attachments = [{
                        "filename": f"{numero}.pdf",
                        "content": base64.b64encode(pdf_resp.content).decode("utf-8"),
                    }]
            except Exception as e:
                logger.error(f"Erreur téléchargement PDF pour email: {e}")
        
        email_data = {
            "from": f"{nom_entreprise} <devis@vocario.fr>",
            "to": [to_email],
            "subject": f"Devis {numero}" + (f" - {titre_projet}" if titre_projet else ""),
            "html": html,
        }
        if attachments:
            email_data["attachments"] = attachments
        
        result = resend.Emails.send(email_data)
        logger.info(f"Email envoyé à {to_email}: {result}")
        return True
    except Exception as e:
        logger.error(f"Erreur envoi email: {e}")
        return False


def send_email_facture(to_email: str, entreprise: Dict, facture: Dict):
    """Envoie une facture par email"""
    if not RESEND_API_KEY:
        return False
    
    nom_entreprise = entreprise.get("nom", "")
    couleur = entreprise.get("couleur_pdf", "#2F665B")
    numero = facture.get("numero_facture", "")
    client_nom = facture.get("client_nom", "")
    total_ttc = facture.get("total_ttc", 0)
    pdf_url = facture.get("pdf_url", "")
    
    html = f'''
    <div style="max-width:600px; margin:0 auto; font-family:Arial,sans-serif;">
        <div style="background-color:{couleur}; padding:20px; text-align:center;">
            <h1 style="color:white; margin:0;">{nom_entreprise}</h1>
        </div>
        <div style="padding:30px; background:#f9f9f9;">
            <p>Bonjour {client_nom},</p>
            <p>Veuillez trouver ci-joint votre facture <strong>{numero}</strong>.</p>
            <div style="background:white; padding:15px; border-radius:8px; text-align:center; margin:20px 0;">
                <p style="color:#666; margin:0;">Montant Total TTC</p>
                <p style="font-size:28px; font-weight:bold; color:{couleur}; margin:5px 0;">{total_ttc:.2f} €</p>
            </div>
            <p>Cordialement,<br/><strong>{nom_entreprise}</strong></p>
        </div>
        <div style="text-align:center; padding:10px; color:#999; font-size:12px;">
            Envoyé via Vocario
        </div>
    </div>
    '''
    
    try:
        attachments = []
        if pdf_url and pdf_url.startswith("http"):
            try:
                pdf_resp = requests.get(pdf_url, timeout=15)
                if pdf_resp.status_code == 200:
                    import base64
                    attachments = [{
                        "filename": f"{numero}.pdf",
                        "content": base64.b64encode(pdf_resp.content).decode("utf-8"),
                    }]
            except Exception as e:
                logger.error(f"Erreur téléchargement PDF facture: {e}")
        
        email_data = {
            "from": f"{nom_entreprise} <facture@vocario.fr>",
            "to": [to_email],
            "subject": f"Facture {numero}",
            "html": html,
        }
        if attachments:
            email_data["attachments"] = attachments
        
        resend.Emails.send(email_data)
        return True
    except Exception as e:
        logger.error(f"Erreur envoi email facture: {e}")
        return False


# =============================================================================
# FONCTIONS DB HELPERS
# =============================================================================

def get_entreprise(phone: str) -> Optional[Dict]:
    """Récupère l'entreprise depuis le numéro WhatsApp"""
    return get_entreprise_by_whatsapp(phone)


def get_devis_list(entreprise_id: str, limit: int = 10) -> List[Dict]:
    """Récupère les devis avec leurs factures associées"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("id, numero_devis, client_nom, client_email, telephone_client, total_ht, total_ttc, statut, date, titre_projet, pdf_url, word_url, remise_type, remise_value")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        
        devis_list = result.data or []
        
        # Pour chaque devis, récupérer les factures associées
        for d in devis_list:
            try:
                fac_result = supabase_client.table("factures")\
                    .select("id, numero_facture, total_ttc, statut, type_facture, date, pdf_url")\
                    .eq("devis_id", d["id"])\
                    .is_("deleted_at", "null")\
                    .order("created_at", desc=True)\
                    .execute()
                d["factures"] = fac_result.data or []
            except:
                d["factures"] = []
        
        return devis_list
    except Exception as e:
        logger.error(f"Erreur get_devis_list: {e}")
        return []


def get_factures_list(entreprise_id: str, limit: int = 10) -> List[Dict]:
    """Récupère les factures orphelines (sans devis_id)"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("factures")\
            .select("id, numero_facture, client_nom, total_ttc, statut, type_facture, date, pdf_url, devis_id")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .is_("devis_id", "null")\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Erreur get_factures_list: {e}")
        return []


def soft_delete_document(table: str, doc_id: str) -> bool:
    """Soft delete un document (devis ou facture)"""
    if not supabase_client:
        return False
    try:
        supabase_client.table(table).update({
            "deleted_at": datetime.now().isoformat()
        }).eq("id", doc_id).execute()
        logger.info(f"Document supprimé: {table}/{doc_id}")
        return True
    except Exception as e:
        logger.error(f"Erreur suppression {table}/{doc_id}: {e}")
        return False


def update_document_status(table: str, doc_id: str, statut: str) -> bool:
    """Met à jour le statut d'un document"""
    if not supabase_client:
        return False
    try:
        supabase_client.table(table).update({
            "statut": statut
        }).eq("id", doc_id).execute()
        return True
    except Exception as e:
        logger.error(f"Erreur update statut {table}/{doc_id}: {e}")
        return False


def get_devis_for_facture(entreprise_id: str) -> List[Dict]:
    """Récupère les devis éligibles pour facturation"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("id, numero_devis, client_nom, client_email, telephone_client, client_adresse, total_ht, total_ttc, statut, prestations, titre_projet, remise_type, remise_value")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(15)\
            .execute()
        
        devis_list = result.data or []
        
        # Ajouter info factures existantes
        for d in devis_list:
            try:
                fac = supabase_client.table("factures")\
                    .select("id, numero_facture, total_ttc, statut, type_facture")\
                    .eq("devis_id", d["id"])\
                    .is_("deleted_at", "null")\
                    .execute()
                d["factures"] = fac.data or []
            except:
                d["factures"] = []
        
        return devis_list
    except Exception as e:
        logger.error(f"Erreur get_devis_for_facture: {e}")
        return []


# =============================================================================
# IA - PARSING PRESTATIONS (Claude Haiku - chirurgical)
# =============================================================================

def parse_prestations_ia(texte: str) -> List[Dict]:
    """Utilise Claude pour parser les prestations depuis du texte libre"""
    if not anthropic_client:
        logger.error("Anthropic non configuré")
        return []
    
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="""Tu es un parser de prestations BTP. Extrais les prestations du texte.
Réponds UNIQUEMENT en JSON valide, un array d'objets.
Chaque objet: {"description": "...", "quantite": N, "unite": "...", "prix_unitaire": N}
Unités valides: u, m2, m², ml, m, h, forfait, lot, kg, l, jour
Si pas de quantité explicite → quantite: 1, unite: "forfait"
Si le prix semble être un total (ex: "peinture 800€"), mets quantite: 1, prix_unitaire: 800
JAMAIS de texte autour du JSON. JAMAIS de commentaires.""",
            messages=[{"role": "user", "content": texte}],
        )
        
        raw = response.content[0].text.strip()
        # Nettoyer le JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        
        prestations = json.loads(raw)
        if isinstance(prestations, list):
            return prestations
        return []
    except Exception as e:
        logger.error(f"Erreur parsing IA: {e}")
        return []


# =============================================================================
# TRANSCRIPTION AUDIO (Whisper)
# =============================================================================

def transcribe_audio(audio_url: str) -> str:
    """Transcrit un message vocal avec Whisper"""
    if not openai_whisper_client:
        return ""
    try:
        # Télécharger l'audio
        twilio_sid = TWILIO_ACCOUNT_SID
        twilio_token = TWILIO_AUTH_TOKEN
        if twilio_sid and twilio_token:
            resp = requests.get(audio_url, auth=(twilio_sid, twilio_token), timeout=15)
        else:
            resp = requests.get(audio_url, timeout=15)
        
        if resp.status_code != 200:
            return ""
        
        temp_file = f"/tmp/audio_{uuid.uuid4().hex}.ogg"
        with open(temp_file, "wb") as f:
            f.write(resp.content)
        
        with open(temp_file, "rb") as audio_file:
            transcript = openai_whisper_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="fr"
            )
        
        try:
            os.remove(temp_file)
        except:
            pass
        
        return transcript.text.strip()
    except Exception as e:
        logger.error(f"Erreur Whisper: {e}")
        return ""


# =============================================================================
# FORMATTAGE DOCUMENTS
# =============================================================================

def format_statut(statut: str, doc_type: str = "devis") -> str:
    """Formate le statut avec emoji"""
    statut_map = {
        "en_attente": "⏳ En attente",
        "envoye": "📤 Envoyé",
        "signe": "✍️ Signé",
        "accepte": "✅ Accepté",
        "refuse": "❌ Refusé",
        "payee": "💰 Payée",
        "paye": "💰 Payé",
        "annule": "🚫 Annulé",
    }
    return statut_map.get(statut, f"⏳ {statut}")


def format_documents_list(devis_list: List[Dict], factures_orphelines: List[Dict]) -> str:
    """Formate la liste de documents groupés par client, lisible sur WhatsApp"""
    if not devis_list and not factures_orphelines:
        return "📂 *Aucun document pour le moment*\n\nTapez *menu* pour créer un devis.", {}
    
    lines = ["📂 *MES DOCUMENTS*"]
    idx = 1
    doc_index = {}
    
    # ── Grouper les devis par client ──
    clients = {}
    for d in devis_list:
        client = (d.get("client_nom") or "Sans nom").strip().upper()
        if client not in clients:
            clients[client] = []
        clients[client].append(d)
    
    for client_name, devis in clients.items():
        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"👤 *{client_name}*")
        lines.append("")
        
        for d in devis:
            total = d.get("total_ttc", 0)
            statut_raw = d.get("statut", "en_attente")
            projet = d.get("titre_projet", "")
            
            # Emoji statut compact (sans texte)
            statut_emoji = {
                "en_attente": "⏳",
                "envoye": "📤",
                "signe": "✍️",
                "accepte": "✅",
                "refuse": "❌",
                "payee": "💰",
                "paye": "💰",
                "annule": "🚫",
            }.get(statut_raw, "⏳")
            
            # Ligne devis : numéro + projet + montant + statut
            label = projet if projet else d.get("numero_devis", "Devis")
            lines.append(f"*{idx}.* {label} · {total:.0f}€ {statut_emoji}")
            
            doc_index[str(idx)] = {"type": "devis", "data": d}
            idx += 1
            
            # Résumé factures compact (1 ligne max)
            factures = d.get("factures", [])
            if factures:
                nb_total = len(factures)
                nb_payees = sum(1 for f in factures if f.get("statut") in ("payee", "paye"))
                nb_acomptes = sum(1 for f in factures if f.get("type_facture") == "acompte")
                nb_finales = nb_total - nb_acomptes
                
                parts = []
                if nb_acomptes > 0:
                    parts.append(f"{nb_acomptes} acompte{'s' if nb_acomptes > 1 else ''}")
                if nb_finales > 0:
                    parts.append(f"{nb_finales} facture{'s' if nb_finales > 1 else ''}")
                
                summary = " + ".join(parts)
                if nb_payees > 0:
                    summary += f" ({nb_payees} payée{'s' if nb_payees > 1 else ''})"
                
                lines.append(f"     └ {summary}")
    
    # ── Factures orphelines ──
    if factures_orphelines:
        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"🧾 *FACTURES*")
        lines.append("")
        
        for f in factures_orphelines:
            fac_type = "Acompte" if f.get("type_facture") == "acompte" else "Facture"
            statut_raw = f.get("statut", "en_attente")
            statut_emoji = {"en_attente": "⏳", "envoye": "📤", "payee": "💰", "paye": "💰"}.get(statut_raw, "⏳")
            fac_total = f.get("total_ttc", 0)
            client = f.get("client_nom", "")
            
            lines.append(f"*{idx}.* {fac_type} {client} · {fac_total:.0f}€ {statut_emoji}")
            doc_index[str(idx)] = {"type": "facture", "data": f}
            idx += 1
    
    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Tapez un N° (1-{idx-1}) pour gérer_")
    lines.append("_Tapez *menu* pour revenir_")
    
    return "\n".join(lines), doc_index


def format_doc_detail(doc_type: str, doc: Dict, devis_parent: Dict = None) -> str:
    """Formate le détail d'un document avec actions"""
    lines = []
    
    if doc_type == "devis":
        numero = doc.get("numero_devis", "")
        client = doc.get("client_nom", "")
        tel = doc.get("telephone_client", "")
        email = doc.get("client_email", "")
        total = doc.get("total_ttc", 0)
        statut = format_statut(doc.get("statut", "en_attente"))
        projet = doc.get("titre_projet", "")
        
        lines.append(f"📋 *DEVIS {numero}*")
        lines.append(f"👤 {client}")
        if projet:
            lines.append(f"🏗️ {projet}")
        if tel:
            lines.append(f"📞 {tel}")
        if email:
            lines.append(f"📧 {email}")
        lines.append(f"💰 {total:.2f}€ TTC")
        lines.append(f"📊 {statut}")
        
        # Factures liées
        factures = doc.get("factures", [])
        if factures:
            lines.append("\n📎 *Factures liées :*")
            for f in factures:
                ft = "Acompte" if f.get("type_facture") == "acompte" else "Finale"
                fs = format_statut(f.get("statut", ""))
                lines.append(f"  └ {f.get('numero_facture', '')} | {ft} {f.get('total_ttc', 0):.0f}€ | {fs}")
        
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("*1.* 📱 Envoyer par WhatsApp")
        lines.append("*2.* 📧 Envoyer par email")
        lines.append("*3.* 💰 Créer facture d'acompte")
        lines.append("*4.* 🧾 Créer facture finale")
        lines.append("*5.* 🗑️ Supprimer")
        lines.append("*6.* ↩️ Retour")
        
    elif doc_type == "facture":
        numero = doc.get("numero_facture", "")
        client = doc.get("client_nom", "")
        total = doc.get("total_ttc", 0)
        statut = format_statut(doc.get("statut", "en_attente"), "facture")
        fac_type = "Acompte" if doc.get("type_facture") == "acompte" else "Facture"
        
        lines.append(f"🧾 *{fac_type.upper()} {numero}*")
        lines.append(f"👤 {client}")
        lines.append(f"💰 {total:.2f}€ TTC")
        lines.append(f"📊 {statut}")
        
        if devis_parent:
            lines.append(f"📎 Devis : {devis_parent.get('numero_devis', '')}")
        
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("*1.* 📱 Envoyer par WhatsApp")
        lines.append("*2.* 📧 Envoyer par email")
        lines.append("*3.* ✅ Marquer comme payée")
        lines.append("*4.* 🗑️ Supprimer")
        lines.append("*5.* ↩️ Retour")
    
    return "\n".join(lines)


# =============================================================================
# HANDLER PRINCIPAL - STATE MACHINE
# =============================================================================

def handle_message(phone: str, message: str, media_url: str = None, media_type: str = None, button_payload: str = None):
    """Gère un message WhatsApp entrant"""
    phone = normalize_phone(phone)
    phone_full = f"+{phone}"
    msg = (message or "").strip()
    msg_lower = msg.lower()
    
    # Audio → transcription Whisper
    if media_url and media_type and ("audio" in media_type or "ogg" in media_type):
        logger.info(f"Message vocal de {phone}")
        transcribed = transcribe_audio(media_url)
        if transcribed:
            msg = transcribed
            msg_lower = msg.lower()
            send_whatsapp(phone_full, f"🎤 _\"{msg}\"_")
        else:
            send_whatsapp(phone_full, "❌ Impossible de comprendre le vocal. Réessayez ou écrivez.\n\n_Tapez *menu* pour le menu principal_")
            return
    
    if not msg and not button_payload:
        send_whatsapp(phone_full, "👋 Tapez *menu* pour commencer !")
        return
    
    conv = get_conv(phone)
    state = conv.get("state", State.MENU)
    data = conv.get("data", {})
    
    logger.info(f"[{phone}] state={state} msg='{msg_lower[:50]}' button={button_payload}")
    
    # =========================================================================
    # COMMANDES GLOBALES (n'importe quel état)
    # =========================================================================
    
    if msg_lower in ["menu", "start", "bonjour", "salut", "hello", "accueil", "0"]:
        reset_conv(phone)
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    if msg_lower in ["annuler", "cancel", "stop"]:
        reset_conv(phone)
        send_whatsapp(phone_full, "❌ Annulé.\n\n_Tapez *menu* pour recommencer._")
        return
    
    if msg_lower == "retour":
        retour_map = {
            State.DEVIS_TEL: State.DEVIS_NOM,
            State.DEVIS_EMAIL: State.DEVIS_TEL,
            State.DEVIS_ADRESSE: State.DEVIS_EMAIL,
            State.DEVIS_PROJET: State.DEVIS_ADRESSE,
            State.DEVIS_PRESTATIONS: State.DEVIS_PROJET,
            State.DEVIS_OPTIONS: State.DEVIS_PRESTATIONS,
            State.DEVIS_RECAP: State.DEVIS_OPTIONS,
            State.DOCS_DETAIL: State.DOCS_LISTE,
        }
        if state in retour_map:
            conv["state"] = retour_map[state]
            save_conv(phone, conv)
            handle_message(phone, "__show__")
            return
        else:
            reset_conv(phone)
            send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
            return
    
    # =========================================================================
    # MENU PRINCIPAL
    # =========================================================================
    
    if state == State.MENU:
        # Boutons template
        if button_payload in ["nouveau_devis", "new_devis", "Nouveau devis"] or msg_lower in ["1", "devis", "nouveau devis", "nouveau", "créer devis"]:
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            send_whatsapp(phone_full, """📝 *NOUVEAU DEVIS*

━━━━━━━━━━━━━━━━━━
*Étape 1/7* - Nom du client
━━━━━━━━━━━━━━━━━━

Quel est le *nom du client* ?

_Exemple: M. Dupont_
_Tapez *annuler* pour annuler_""")
            return
        
        if button_payload in ["nouvelle_facture", "new_facture", "Nouvelle facture"] or msg_lower in ["2", "facture", "nouvelle facture"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "❌ Entreprise non trouvée. Configurez votre profil sur vocario.fr\n\n_Tapez *menu* pour revenir_")
                return
            
            devis_list = get_devis_for_facture(entreprise["id"])
            if not devis_list:
                send_whatsapp(phone_full, "📭 Aucun devis trouvé. Créez d'abord un devis !\n\n_Tapez *menu* pour revenir_")
                return
            
            # Afficher la liste des devis pour facturation
            lines = ["🧾 *NOUVELLE FACTURE*\n", "Choisissez le devis à facturer :\n"]
            for i, d in enumerate(devis_list, 1):
                client = d.get("client_nom", "")
                total = d.get("total_ttc", 0)
                factures = d.get("factures", [])
                
                # Résumé des factures existantes
                acomptes_payes = sum(f.get("total_ttc", 0) for f in factures if f.get("statut") == "payee" and f.get("type_facture") == "acompte")
                has_finale = any(f.get("type_facture") != "acompte" for f in factures)
                
                info = f"*{i}.* {client} | {total:.0f}€"
                if has_finale:
                    info += " | ✅ Déjà facturé"
                elif acomptes_payes > 0:
                    info += f" | 💰 Acompte {acomptes_payes:.0f}€ payé"
                elif factures:
                    info += " | ⏳ Acompte en attente"
                else:
                    info += " | Pas encore facturé"
                lines.append(info)
            
            lines.append(f"\n_Tapez le numéro (1-{len(devis_list)})_")
            lines.append("_Tapez *menu* pour revenir_")
            
            conv["state"] = State.FACTURE_LISTE
            conv["data"] = {"devis_options": devis_list}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        if button_payload in ["mes_documents", "documents", "Mes documents"] or msg_lower in ["3", "documents", "mes documents", "docs", "mes docs"]:
            _show_documents(phone, phone_full, conv)
            return
        
        # Message libre depuis le menu → re-envoyer le menu
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    # =========================================================================
    # FLOW DEVIS - ÉTAPES
    # =========================================================================
    
    if state == State.DEVIS_NOM:
        if msg == "__show__":
            send_whatsapp(phone_full, "📝 *Étape 1/7* - Nom du client\n\nQuel est le *nom du client* ?")
            return
        data["client_nom"] = msg
        conv["data"] = data
        conv["state"] = State.DEVIS_TEL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"""✅ Client : *{msg}*

━━━━━━━━━━━━━━━━━━
*Étape 2/7* - Téléphone
━━━━━━━━━━━━━━━━━━

Quel est son *numéro de téléphone* ?

_Exemple: 06 12 34 56 78_
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_TEL:
        if msg == "__show__":
            send_whatsapp(phone_full, f"Client: {data.get('client_nom', '')}\n\n📝 *Étape 2/7* - Téléphone\n\nQuel est son *numéro* ?")
            return
        tel = re.sub(r'[^0-9+]', '', msg)
        if len(tel) < 10:
            send_whatsapp(phone_full, "❌ Numéro invalide (minimum 10 chiffres).\n\n_Exemple: 0612345678_")
            return
        data["client_tel"] = tel
        conv["data"] = data
        conv["state"] = State.DEVIS_EMAIL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"""✅ Téléphone : *{tel}*

━━━━━━━━━━━━━━━━━━
*Étape 3/7* - Email (optionnel)
━━━━━━━━━━━━━━━━━━

Quel est son *email* ?

_Tapez *non* si pas d'email_
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_EMAIL:
        if msg == "__show__":
            send_whatsapp(phone_full, "📝 *Étape 3/7* - Email\n\nQuel est son *email* ?\n_Tapez *non* si pas d'email_")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_email"] = ""
        elif "@" in msg and "." in msg:
            data["client_email"] = msg.lower().strip()
        else:
            send_whatsapp(phone_full, "⚠️ Email invalide.\n\nEntrez un email valide ou tapez *non*")
            return
        
        conv["data"] = data
        conv["state"] = State.DEVIS_ADRESSE
        save_conv(phone, conv)
        email_txt = data["client_email"] or "Non renseigné"
        send_whatsapp(phone_full, f"""✅ Email : *{email_txt}*

━━━━━━━━━━━━━━━━━━
*Étape 4/7* - Adresse (optionnel)
━━━━━━━━━━━━━━━━━━

Quelle est l'*adresse du chantier/client* ?

_Tapez *non* si pas d'adresse_
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_ADRESSE:
        if msg == "__show__":
            send_whatsapp(phone_full, "📝 *Étape 4/7* - Adresse\n\nQuelle est l'*adresse* ?\n_Tapez *non* si pas d'adresse_")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_adresse"] = ""
        else:
            data["client_adresse"] = msg
        
        conv["data"] = data
        conv["state"] = State.DEVIS_PROJET
        save_conv(phone, conv)
        addr_txt = data["client_adresse"] or "Non renseigné"
        send_whatsapp(phone_full, f"""✅ Adresse : *{addr_txt}*

━━━━━━━━━━━━━━━━━━
*Étape 5/7* - Nom du projet
━━━━━━━━━━━━━━━━━━

Quel est le *nom du projet* ?

_Exemple: Rénovation salle de bain_
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_PROJET:
        if msg == "__show__":
            send_whatsapp(phone_full, "📝 *Étape 5/7* - Projet\n\nQuel est le *nom du projet* ?")
            return
        data["titre_projet"] = msg
        conv["data"] = data
        conv["state"] = State.DEVIS_PRESTATIONS
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"""✅ Projet : *{msg}*

━━━━━━━━━━━━━━━━━━
*Étape 6/7* - Prestations
━━━━━━━━━━━━━━━━━━

Décrivez les *travaux avec les prix* :

_Exemples :_
• _Carrelage 30m² 50€_
• _Peinture salon forfait 800€_
• _Main d'œuvre 10h 45€_

Envoyez tout en un message ou un vocal 🎤
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_PRESTATIONS:
        if msg == "__show__":
            send_whatsapp(phone_full, "📝 *Étape 6/7* - Prestations\n\nDécrivez les *travaux avec les prix*\n_Envoyez tout en un message ou un vocal 🎤_")
            return
        
        # Parser les prestations avec l'IA
        send_whatsapp(phone_full, "⏳ Analyse en cours...")
        prestations = parse_prestations_ia(msg)
        
        if not prestations:
            send_whatsapp(phone_full, "❌ Je n'ai pas compris les prestations.\n\nEssayez comme ça :\n_Carrelage 30m² 50€_\n_Peinture forfait 800€_")
            return
        
        data["prestations"] = prestations
        
        # Calculer total HT
        total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in prestations)
        
        # Afficher les prestations parsées
        lines = ["✅ *Prestations enregistrées :*\n"]
        for p in prestations:
            qte = p.get("quantite", 1)
            unite = p.get("unite", "u")
            pu = p.get("prix_unitaire", 0)
            desc = p.get("description", "")
            total_l = qte * pu
            if qte == 1 and unite in ["forfait", "u"]:
                lines.append(f"• {desc} = {total_l:.0f}€")
            else:
                lines.append(f"• {desc} {qte} {unite} × {pu:.0f}€ = {total_l:.0f}€")
        
        lines.append(f"\n💰 *Total HT : {total_ht:.2f}€*")
        lines.append("\n*1.* ➕ Ajouter une prestation")
        lines.append("*2.* ✅ Continuer")
        lines.append("*3.* 🔄 Refaire les prestations")
        lines.append("_Tapez *retour* pour modifier_")
        
        conv["data"] = data
        conv["state"] = State.DEVIS_PRESTATIONS_SUITE
        save_conv(phone, conv)
        send_whatsapp(phone_full, "\n".join(lines))
        return
    
    if state == State.DEVIS_PRESTATIONS_SUITE:
        if msg_lower in ["2", "continuer", "ok", "oui", "valider"]:
            conv["state"] = State.DEVIS_OPTIONS
            save_conv(phone, conv)
            send_whatsapp(phone_full, """━━━━━━━━━━━━━━━━━━
*Étape 7/7* - Options
━━━━━━━━━━━━━━━━━━

Souhaitez-vous ajouter :

*1.* 🏷️ Remise
*2.* 💰 Acompte
*3.* ⏱️ Délai de réalisation
*4.* ⏭️ Passer (pas d'option)""")
            return
        
        if msg_lower in ["3", "refaire"]:
            conv["state"] = State.DEVIS_PRESTATIONS
            save_conv(phone, conv)
            handle_message(phone, "__show__")
            return
        
        if msg_lower in ["1", "ajouter"]:
            send_whatsapp(phone_full, "➕ Envoyez la prestation à ajouter :\n\n_Exemple: Plomberie forfait 500€_")
            conv["state"] = State.DEVIS_PRESTATIONS  # Re-parser, ça ajoutera
            # Garder les prestations existantes pour le prochain parsing
            conv["data"]["_prestations_precedentes"] = data.get("prestations", [])
            save_conv(phone, conv)
            return
        
        send_whatsapp(phone_full, "Tapez *1* (ajouter), *2* (continuer) ou *3* (refaire)")
        return
    
    if state == State.DEVIS_OPTIONS:
        if msg_lower in ["1", "remise"]:
            conv["state"] = State.DEVIS_REMISE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "🏷️ Quel *pourcentage de remise* ?\n\n_Exemple: 10_")
            return
        
        if msg_lower in ["2", "acompte"]:
            conv["state"] = State.DEVIS_ACOMPTE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 Quel *pourcentage d'acompte* ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre pourcentage")
            return
        
        if msg_lower in ["3", "delai", "délai"]:
            conv["state"] = State.DEVIS_DELAI
            save_conv(phone, conv)
            send_whatsapp(phone_full, "⏱️ Quel *délai de réalisation* ?\n\n_Exemple: 2 semaines_")
            return
        
        if msg_lower in ["4", "passer", "non", "rien"]:
            _show_recap(phone, phone_full, conv)
            return
        
        send_whatsapp(phone_full, "Tapez *1* (remise), *2* (acompte), *3* (délai) ou *4* (passer)")
        return
    
    if state == State.DEVIS_REMISE:
        try:
            remise = float(msg.replace("%", "").replace(",", ".").strip())
            if 0 < remise <= 100:
                data["remise_type"] = "pourcentage"
                data["remise_valeur"] = remise
                conv["data"] = data
                conv["state"] = State.DEVIS_OPTIONS
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"✅ Remise *{remise}%* ajoutée !\n\nAutre option ?\n*2.* 💰 Acompte\n*3.* ⏱️ Délai\n*4.* ⏭️ Passer")
                return
        except:
            pass
        send_whatsapp(phone_full, "❌ Nombre invalide. Entrez un pourcentage (ex: 10)")
        return
    
    if state == State.DEVIS_ACOMPTE:
        acompte = 0
        if msg_lower in ["1", "30", "30%"]:
            acompte = 30
        elif msg_lower in ["2", "40", "40%"]:
            acompte = 40
        elif msg_lower in ["3", "50", "50%"]:
            acompte = 50
        else:
            try:
                acompte = float(msg.replace("%", "").replace(",", ".").strip())
            except:
                send_whatsapp(phone_full, "❌ Nombre invalide. Tapez *1* (30%), *2* (40%), *3* (50%) ou un nombre")
                return
        
        if 0 < acompte <= 100:
            data["acompte_pourcentage"] = acompte
            conv["data"] = data
            conv["state"] = State.DEVIS_OPTIONS
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"✅ Acompte *{acompte}%* ajouté !\n\nAutre option ?\n*1.* 🏷️ Remise\n*3.* ⏱️ Délai\n*4.* ⏭️ Passer")
            return
        send_whatsapp(phone_full, "❌ Nombre invalide (1-100)")
        return
    
    if state == State.DEVIS_DELAI:
        data["delai"] = msg
        conv["data"] = data
        conv["state"] = State.DEVIS_OPTIONS
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ Délai : *{msg}*\n\nAutre option ?\n*1.* 🏷️ Remise\n*2.* 💰 Acompte\n*4.* ⏭️ Passer")
        return
    
    if state == State.DEVIS_RECAP:
        if msg_lower in ["1", "valider", "ok", "oui", "confirmer", "go"]:
            _generate_devis(phone, phone_full, conv)
            return
        if msg_lower in ["2", "modifier"]:
            conv["state"] = State.DEVIS_MODIFIER
            save_conv(phone, conv)
            send_whatsapp(phone_full, """✏️ *Que voulez-vous modifier ?*

*1.* Nom du client
*2.* Téléphone
*3.* Email
*4.* Adresse
*5.* Projet
*6.* Prestations
*7.* Remise/Acompte/Délai
*8.* ❌ Annuler le devis""")
            return
        if msg_lower in ["3", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Devis annulé.\n\n_Tapez *menu* pour recommencer._")
            return
        send_whatsapp(phone_full, "Tapez *1* (valider), *2* (modifier) ou *3* (annuler)")
        return
    
    if state == State.DEVIS_MODIFIER:
        modify_map = {
            "1": State.DEVIS_NOM, "2": State.DEVIS_TEL, "3": State.DEVIS_EMAIL,
            "4": State.DEVIS_ADRESSE, "5": State.DEVIS_PROJET, "6": State.DEVIS_PRESTATIONS,
            "7": State.DEVIS_OPTIONS,
        }
        if msg_lower in modify_map:
            conv["state"] = modify_map[msg_lower]
            save_conv(phone, conv)
            handle_message(phone, "__show__")
            return
        if msg_lower == "8":
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Devis annulé.\n\n_Tapez *menu* pour recommencer._")
            return
        send_whatsapp(phone_full, "Tapez un numéro (1-8)")
        return
    
    # =========================================================================
    # DEVIS GÉNÉRÉ - ACTIONS POST-CRÉATION
    # =========================================================================
    
    if state == State.DEVIS_GENERE:
        devis_info = data.get("devis_genere", {})
        
        if msg_lower in ["1", "whatsapp", "envoyer"]:
            # Envoyer par WhatsApp au client
            tel_client = devis_info.get("client_tel") or data.get("client_tel", "")
            if tel_client:
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = devis_info
                conv["data"]["send_doc"]["default_tel"] = tel_client
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"""📱 *Envoi WhatsApp*

Client : {devis_info.get('client_nom', '')}
Numéro : *{tel_client}*

*1.* ✅ Envoyer à ce numéro
*2.* 📝 Autre numéro
*3.* ❌ Annuler""")
                return
            else:
                send_whatsapp(phone_full, "📱 Entrez le numéro du client :\n\n_Exemple: 0612345678_")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = devis_info
                save_conv(phone, conv)
                return
        
        if msg_lower in ["2", "email"]:
            email_client = devis_info.get("client_email") or data.get("client_email", "")
            conv["state"] = State.DOCS_SIGNATURE_CHOIX
            conv["data"]["send_doc"] = devis_info
            conv["data"]["send_doc"]["default_email"] = email_client
            conv["data"]["send_doc"]["doc_type"] = "devis"
            save_conv(phone, conv)
            
            if email_client:
                send_whatsapp(phone_full, f"""📧 *Envoi Email*

Client : {devis_info.get('client_nom', '')}
Email : *{email_client}*

*1.* ✍️ Avec signature électronique
*2.* 📄 Sans signature (PDF seul)
*3.* 📝 Autre email
*4.* ❌ Annuler""")
            else:
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                save_conv(phone, conv)
            return
        
        if msg_lower in ["3", "nouveau", "nouveau devis"]:
            reset_conv(phone)
            conv = get_conv(phone)
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            handle_message(phone, "__show__")
            return
        
        if msg_lower in ["4", "facture", "acompte"]:
            # Créer facture acompte directement
            conv["state"] = State.FACTURE_ACOMPTE_TAUX
            conv["data"]["selected_devis"] = devis_info
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\nQuel pourcentage ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre")
            return
        
        if msg_lower in ["5", "menu"]:
            reset_conv(phone)
            send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
            return
        
        send_whatsapp(phone_full, "Tapez *1* (WhatsApp), *2* (email), *3* (nouveau devis), *4* (facture acompte) ou *5* (menu)")
        return
    
    # =========================================================================
    # FLOW FACTURE
    # =========================================================================
    
    if state == State.FACTURE_LISTE:
        devis_options = data.get("devis_options", [])
        try:
            idx = int(msg) - 1
            if 0 <= idx < len(devis_options):
                selected = devis_options[idx]
                data["selected_devis"] = selected
                
                # Vérifier s'il y a déjà une facture finale
                has_finale = any(f.get("type_facture") != "acompte" for f in selected.get("factures", []))
                if has_finale:
                    send_whatsapp(phone_full, f"⚠️ Ce devis a déjà une facture finale.\n\n_Tapez *menu* pour revenir_")
                    return
                
                conv["data"] = data
                conv["state"] = State.FACTURE_TYPE
                save_conv(phone, conv)
                
                acomptes = selected.get("factures", [])
                acomptes_payes = sum(f.get("total_ttc", 0) for f in acomptes if f.get("statut") == "payee")
                total_ttc = selected.get("total_ttc", 0)
                
                lines = [f"📋 *{selected.get('numero_devis', '')}* | {selected.get('client_nom', '')}", f"💰 Total : {total_ttc:.0f}€ TTC\n"]
                
                if acomptes_payes > 0:
                    reste = total_ttc - acomptes_payes
                    lines.append(f"✅ Acomptes payés : {acomptes_payes:.0f}€")
                    lines.append(f"📊 Reste : {reste:.0f}€\n")
                
                lines.append("*1.* 💰 Facture d'acompte")
                lines.append("*2.* 🧾 Facture finale (solde)")
                lines.append("*3.* ↩️ Retour")
                
                send_whatsapp(phone_full, "\n".join(lines))
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, "❌ Numéro invalide. Tapez un numéro de la liste.")
        return
    
    if state == State.FACTURE_TYPE:
        if msg_lower in ["1", "acompte"]:
            conv["state"] = State.FACTURE_ACOMPTE_TAUX
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\nQuel pourcentage ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre")
            return
        
        if msg_lower in ["2", "finale", "solde"]:
            _generate_facture_finale(phone, phone_full, conv)
            return
        
        if msg_lower in ["3", "retour"]:
            conv["state"] = State.FACTURE_LISTE
            save_conv(phone, conv)
            handle_message(phone, "2")  # Re-afficher la liste facture
            return
        
        send_whatsapp(phone_full, "Tapez *1* (acompte), *2* (finale) ou *3* (retour)")
        return
    
    if state == State.FACTURE_ACOMPTE_TAUX:
        taux = 0
        if msg_lower in ["1", "30", "30%"]:
            taux = 30
        elif msg_lower in ["2", "40", "40%"]:
            taux = 40
        elif msg_lower in ["3", "50", "50%"]:
            taux = 50
        else:
            try:
                taux = float(msg.replace("%", "").strip())
            except:
                send_whatsapp(phone_full, "❌ Nombre invalide. Tapez *1* (30%), *2* (40%), *3* (50%) ou un nombre")
                return
        
        if 0 < taux <= 100:
            _generate_facture_acompte(phone, phone_full, conv, taux)
            return
        send_whatsapp(phone_full, "❌ Pourcentage invalide (1-100)")
        return
    
    if state == State.FACTURE_GENERE:
        facture_info = data.get("facture_genere", {})
        
        if msg_lower in ["1", "whatsapp"]:
            tel = facture_info.get("client_tel", "") or data.get("selected_devis", {}).get("telephone_client", "")
            conv["state"] = State.DOCS_ENVOYER_WA
            conv["data"]["send_doc"] = facture_info
            conv["data"]["send_doc"]["default_tel"] = tel
            save_conv(phone, conv)
            if tel:
                send_whatsapp(phone_full, f"📱 Envoyer la facture à *{tel}* ?\n\n*1.* ✅ Oui\n*2.* 📝 Autre numéro\n*3.* ❌ Annuler")
            else:
                send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
            return
        
        if msg_lower in ["2", "email"]:
            email = facture_info.get("client_email", "") or data.get("selected_devis", {}).get("client_email", "")
            conv["state"] = State.DOCS_ENVOYER_EMAIL
            conv["data"]["send_doc"] = facture_info
            conv["data"]["send_doc"]["default_email"] = email
            conv["data"]["send_doc"]["doc_type"] = "facture"
            save_conv(phone, conv)
            if email:
                send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✅ Oui\n*2.* 📝 Autre email\n*3.* ❌ Annuler")
            else:
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            return
        
        if msg_lower in ["3", "payee", "payé", "payer"]:
            fac_id = facture_info.get("id", "")
            if fac_id and update_document_status("factures", fac_id, "payee"):
                send_whatsapp(phone_full, "✅ Facture marquée comme *payée* !\n\n_Tapez *menu* pour revenir_")
            else:
                send_whatsapp(phone_full, "❌ Erreur. Réessayez.\n\n_Tapez *menu* pour revenir_")
            reset_conv(phone)
            return
        
        if msg_lower in ["4", "menu"]:
            reset_conv(phone)
            send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
            return
        
        send_whatsapp(phone_full, "Tapez *1* (WhatsApp), *2* (email), *3* (marquer payée) ou *4* (menu)")
        return
    
    # =========================================================================
    # DOCUMENTS
    # =========================================================================
    
    if state == State.DOCS_LISTE:
        doc_index = data.get("doc_index", {})
        
        if msg_lower in doc_index:
            doc_entry = doc_index[msg_lower]
            data["current_doc"] = doc_entry
            conv["data"] = data
            conv["state"] = State.DOCS_DETAIL
            save_conv(phone, conv)
            
            detail = format_doc_detail(doc_entry["type"], doc_entry["data"], doc_entry.get("devis"))
            send_whatsapp(phone_full, detail)
            return
        
        send_whatsapp(phone_full, "❌ Numéro invalide. Tapez un numéro de la liste ou *menu*.")
        return
    
    if state == State.DOCS_DETAIL:
        doc_entry = data.get("current_doc", {})
        doc_type = doc_entry.get("type", "")
        doc = doc_entry.get("data", {})
        devis_parent = doc_entry.get("devis")
        
        # DEVIS actions
        if doc_type == "devis":
            if msg_lower in ["1", "whatsapp"]:
                tel = doc.get("telephone_client", "")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_devis", ""), "client_nom": doc.get("client_nom", ""), "default_tel": tel, "doc_type": "devis"}
                save_conv(phone, conv)
                if tel:
                    send_whatsapp(phone_full, f"📱 Envoyer à *{tel}* ?\n\n*1.* ✅ Oui\n*2.* 📝 Autre numéro\n*3.* ❌ Annuler")
                else:
                    send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
                return
            
            if msg_lower in ["2", "email"]:
                email = doc.get("client_email", "")
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_devis", ""), "id": doc.get("id", ""), "client_nom": doc.get("client_nom", ""), "default_email": email, "doc_type": "devis", "total_ttc": doc.get("total_ttc", 0), "titre_projet": doc.get("titre_projet", "")}
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 *Envoi Email* à *{email}*\n\n*1.* ✍️ Avec signature électronique\n*2.* 📄 Sans signature (PDF seul)\n*3.* 📝 Autre email\n*4.* ❌ Annuler")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                    conv["state"] = State.DOCS_ENVOYER_EMAIL
                    save_conv(phone, conv)
                return
            
            if msg_lower in ["3", "acompte"]:
                conv["state"] = State.FACTURE_ACOMPTE_TAUX
                conv["data"]["selected_devis"] = doc
                save_conv(phone, conv)
                send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\nQuel pourcentage ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre")
                return
            
            if msg_lower in ["4", "finale"]:
                conv["data"]["selected_devis"] = doc
                save_conv(phone, conv)
                _generate_facture_finale(phone, phone_full, conv)
                return
            
            if msg_lower in ["5", "supprimer"]:
                conv["state"] = State.DOCS_CONFIRMER_SUPPR
                conv["data"]["suppr_doc"] = {"type": "devis", "id": doc.get("id", ""), "numero": doc.get("numero_devis", "")}
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"🗑️ *Confirmer la suppression ?*\n\nDevis {doc.get('numero_devis', '')} - {doc.get('client_nom', '')}\n\n⚠️ Les factures liées seront aussi supprimées.\n\n*1.* ✅ Oui, supprimer\n*2.* ❌ Non, annuler")
                return
            
            if msg_lower in ["6", "retour"]:
                _show_documents(phone, phone_full, conv)
                return
        
        # FACTURE actions
        elif doc_type == "facture":
            if msg_lower in ["1", "whatsapp"]:
                tel = doc.get("client_telephone", "") or (devis_parent or {}).get("telephone_client", "")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_facture", ""), "client_nom": doc.get("client_nom", ""), "default_tel": tel, "doc_type": "facture"}
                save_conv(phone, conv)
                if tel:
                    send_whatsapp(phone_full, f"📱 Envoyer à *{tel}* ?\n\n*1.* ✅ Oui\n*2.* 📝 Autre numéro\n*3.* ❌ Annuler")
                else:
                    send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
                return
            
            if msg_lower in ["2", "email"]:
                email = doc.get("client_email", "") or (devis_parent or {}).get("client_email", "")
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_facture", ""), "client_nom": doc.get("client_nom", ""), "default_email": email, "doc_type": "facture", "total_ttc": doc.get("total_ttc", 0)}
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✅ Oui\n*2.* 📝 Autre email\n*3.* ❌ Annuler")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                return
            
            if msg_lower in ["3", "payee", "payé"]:
                fac_id = doc.get("id", "")
                if fac_id and update_document_status("factures", fac_id, "payee"):
                    send_whatsapp(phone_full, "✅ Facture marquée comme *payée* !\n\n_Tapez *menu* pour revenir_")
                else:
                    send_whatsapp(phone_full, "❌ Erreur.\n\n_Tapez *menu* pour revenir_")
                reset_conv(phone)
                return
            
            if msg_lower in ["4", "supprimer"]:
                conv["state"] = State.DOCS_CONFIRMER_SUPPR
                conv["data"]["suppr_doc"] = {"type": "facture", "id": doc.get("id", ""), "numero": doc.get("numero_facture", "")}
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"🗑️ *Confirmer la suppression ?*\n\nFacture {doc.get('numero_facture', '')}\n\n*1.* ✅ Oui, supprimer\n*2.* ❌ Non, annuler")
                return
            
            if msg_lower in ["5", "retour"]:
                _show_documents(phone, phone_full, conv)
                return
        
        send_whatsapp(phone_full, "Tapez un numéro d'action ou *menu*")
        return
    
    # =========================================================================
    # ENVOI WHATSAPP AU CLIENT
    # =========================================================================
    
    if state == State.DOCS_ENVOYER_WA:
        send_doc = data.get("send_doc", {})
        default_tel = send_doc.get("default_tel", "")
        
        if msg_lower in ["1", "oui"] and default_tel:
            tel = default_tel
        elif msg_lower in ["2", "autre"]:
            send_whatsapp(phone_full, "📱 Entrez le nouveau numéro :")
            data["send_doc"]["default_tel"] = ""  # Reset pour attendre un numéro
            conv["data"] = data
            save_conv(phone, conv)
            return
        elif msg_lower in ["3", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Envoi annulé.\n\n_Tapez *menu* pour revenir_")
            return
        else:
            # C'est un numéro saisi
            tel = re.sub(r'[^0-9+]', '', msg)
            if len(tel) < 10:
                send_whatsapp(phone_full, "❌ Numéro invalide.\n\n_Tapez un numéro valide ou *annuler*_")
                return
        
        # Formater le numéro
        if tel.startswith("0"):
            tel = "33" + tel[1:]
        if not tel.startswith("+"):
            tel = "+" + tel
        
        # Envoyer le document
        pdf_url = send_doc.get("pdf_url", "")
        numero = send_doc.get("numero", "")
        client_nom = send_doc.get("client_nom", "")
        
        if pdf_url and pdf_url.startswith("http"):
            success = send_whatsapp_document(tel, pdf_url, f"📄 {numero}")
            if success:
                # Mettre à jour le statut
                doc_type = send_doc.get("doc_type", "devis")
                doc_id = send_doc.get("id", "")
                if doc_id:
                    table = "devis" if doc_type == "devis" else "factures"
                    update_document_status(table, doc_id, "envoye")
                
                send_whatsapp(phone_full, f"✅ *Document envoyé à {client_nom}* ({tel}) !\n\n_Tapez *menu* pour revenir_")
            else:
                send_whatsapp(phone_full, f"❌ Erreur d'envoi. Réessayez.\n\n_Tapez *menu* pour revenir_")
        else:
            send_whatsapp(phone_full, f"❌ PDF non disponible.\n\n_Tapez *menu* pour revenir_")
        
        reset_conv(phone)
        return
    
    # =========================================================================
    # ENVOI EMAIL - SIGNATURE
    # =========================================================================
    
    if state == State.DOCS_SIGNATURE_CHOIX:
        send_doc = data.get("send_doc", {})
        default_email = send_doc.get("default_email", "")
        
        if msg_lower in ["1", "signature", "avec"]:
            email = default_email
            if not email:
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                conv["data"]["send_doc"]["avec_signature"] = True
                save_conv(phone, conv)
                return
            _send_email_action(phone, phone_full, conv, email, avec_signature=True)
            return
        
        if msg_lower in ["2", "sans", "pdf"]:
            email = default_email
            if not email:
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                conv["data"]["send_doc"]["avec_signature"] = False
                save_conv(phone, conv)
                return
            _send_email_action(phone, phone_full, conv, email, avec_signature=False)
            return
        
        if msg_lower in ["3", "autre"]:
            send_whatsapp(phone_full, "📧 Entrez le nouvel email :")
            conv["state"] = State.DOCS_ENVOYER_EMAIL
            save_conv(phone, conv)
            return
        
        if msg_lower in ["4", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Annulé.\n\n_Tapez *menu* pour revenir_")
            return
        
        send_whatsapp(phone_full, "Tapez *1* (avec signature), *2* (sans), *3* (autre email) ou *4* (annuler)")
        return
    
    if state == State.DOCS_ENVOYER_EMAIL:
        send_doc = data.get("send_doc", {})
        default_email = send_doc.get("default_email", "")
        
        if msg_lower in ["1", "oui"] and default_email:
            _send_email_action(phone, phone_full, conv, default_email)
            return
        
        if msg_lower in ["2", "autre"]:
            send_whatsapp(phone_full, "📧 Entrez le nouvel email :")
            data["send_doc"]["default_email"] = ""
            conv["data"] = data
            save_conv(phone, conv)
            return
        
        if msg_lower in ["3", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Annulé.\n\n_Tapez *menu* pour revenir_")
            return
        
        # C'est un email saisi
        if "@" in msg and "." in msg:
            avec_signature = send_doc.get("avec_signature", False)
            doc_type = send_doc.get("doc_type", "devis")
            
            if doc_type == "devis" and not send_doc.get("_signature_asked"):
                # Demander avec/sans signature
                conv["data"]["send_doc"]["default_email"] = msg.lower().strip()
                conv["data"]["send_doc"]["_signature_asked"] = True
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"📧 Email : *{msg}*\n\n*1.* ✍️ Avec signature électronique\n*2.* 📄 Sans signature\n*3.* ❌ Annuler")
                return
            
            _send_email_action(phone, phone_full, conv, msg.lower().strip(), avec_signature=avec_signature)
            return
        
        send_whatsapp(phone_full, "⚠️ Email invalide. Réessayez ou tapez *annuler*")
        return
    
    # =========================================================================
    # CONFIRMATION SUPPRESSION
    # =========================================================================
    
    if state == State.DOCS_CONFIRMER_SUPPR:
        suppr = data.get("suppr_doc", {})
        
        if msg_lower in ["1", "oui", "confirmer"]:
            doc_type = suppr.get("type", "")
            doc_id = suppr.get("id", "")
            numero = suppr.get("numero", "")
            
            table = "devis" if doc_type == "devis" else "factures"
            if soft_delete_document(table, doc_id):
                # Si c'est un devis, supprimer aussi les factures associées
                if doc_type == "devis" and supabase_client:
                    try:
                        supabase_client.table("factures").update({
                            "deleted_at": datetime.now().isoformat()
                        }).eq("devis_id", doc_id).execute()
                    except:
                        pass
                send_whatsapp(phone_full, f"✅ *{numero}* supprimé !\n\n_Tapez *menu* pour revenir_")
            else:
                send_whatsapp(phone_full, "❌ Erreur de suppression.\n\n_Tapez *menu* pour revenir_")
            reset_conv(phone)
            return
        
        if msg_lower in ["2", "non", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "↩️ Suppression annulée.\n\n_Tapez *menu* pour revenir_")
            return
        
        send_whatsapp(phone_full, "Tapez *1* (supprimer) ou *2* (annuler)")
        return
    
    # =========================================================================
    # ÉTAT INCONNU → MENU
    # =========================================================================
    send_whatsapp(phone_full, "🤔 Je n'ai pas compris.\n\n_Tapez *menu* pour le menu principal_")


# =============================================================================
# FONCTIONS HELPER
# =============================================================================

def _show_documents(phone: str, phone_full: str, conv: Dict):
    """Affiche la liste des documents"""
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "❌ Entreprise non trouvée. Configurez votre profil sur vocario.fr\n\n_Tapez *menu* pour revenir_")
        return
    
    devis_list = get_devis_list(entreprise["id"])
    factures_orphelines = get_factures_list(entreprise["id"])
    
    result = format_documents_list(devis_list, factures_orphelines)
    if isinstance(result, tuple):
        text, doc_index = result
    else:
        text = result
        doc_index = {}
    
    conv["state"] = State.DOCS_LISTE
    conv["data"] = {"doc_index": doc_index}
    save_conv(phone, conv)
    send_whatsapp(phone_full, text)


def _show_recap(phone: str, phone_full: str, conv: Dict):
    """Affiche le récap du devis avant validation"""
    data = conv.get("data", {})
    prestations = data.get("prestations", [])
    
    total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in prestations)
    
    remise_type = data.get("remise_type")
    remise_valeur = data.get("remise_valeur", 0)
    remise_montant = 0
    if remise_type == "pourcentage" and remise_valeur > 0:
        remise_montant = total_ht * (remise_valeur / 100)
    
    total_ht_apres_remise = total_ht - remise_montant
    
    # Récupérer le taux TVA de l'entreprise
    entreprise = get_entreprise(phone)
    tva_taux = 20.0
    if entreprise:
        tva_raw = entreprise.get("tva_taux")
        if tva_raw is not None:
            tva_taux = float(tva_raw)
    
    total_tva = total_ht_apres_remise * (tva_taux / 100)
    total_ttc = total_ht_apres_remise + total_tva
    
    acompte = data.get("acompte_pourcentage", 0)
    acompte_montant = total_ttc * (acompte / 100) if acompte > 0 else 0
    
    lines = ["📋 *RÉCAPITULATIF DEVIS*\n"]
    lines.append(f"👤 *Client :* {data.get('client_nom', '')}")
    if data.get("client_tel"):
        lines.append(f"📞 {data['client_tel']}")
    if data.get("client_email"):
        lines.append(f"📧 {data['client_email']}")
    if data.get("client_adresse"):
        lines.append(f"📍 {data['client_adresse']}")
    if data.get("titre_projet"):
        lines.append(f"🏗️ *Projet :* {data['titre_projet']}")
    
    lines.append("\n*Prestations :*")
    for p in prestations:
        qte = p.get("quantite", 1)
        unite = p.get("unite", "u")
        pu = p.get("prix_unitaire", 0)
        desc = p.get("description", "")
        total_l = qte * pu
        if qte == 1 and unite in ["forfait", "u"]:
            lines.append(f"• {desc} = {total_l:.0f}€")
        else:
            lines.append(f"• {desc} {qte} {unite} × {pu:.0f}€ = {total_l:.0f}€")
    
    lines.append(f"\n💰 *Total HT : {total_ht:.2f}€*")
    
    if remise_montant > 0:
        lines.append(f"🏷️ Remise {remise_valeur}% : -{remise_montant:.2f}€")
        lines.append(f"💰 *Total HT après remise : {total_ht_apres_remise:.2f}€*")
    
    if tva_taux > 0:
        lines.append(f"📊 TVA ({tva_taux}%) : {total_tva:.2f}€")
    else:
        lines.append("📊 _TVA non applicable_")
    
    lines.append(f"💰 *Total TTC : {total_ttc:.2f}€*")
    
    if acompte > 0:
        lines.append(f"\n📅 Acompte demandé : {acompte_montant:.2f}€ ({acompte}%)")
    
    if data.get("delai"):
        lines.append(f"⏱️ Délai : {data['delai']}")
    
    lines.append("\n━━━━━━━━━━━━━━━━━━")
    lines.append("*1.* ✅ Valider et générer")
    lines.append("*2.* ✏️ Modifier")
    lines.append("*3.* ❌ Annuler")
    
    conv["state"] = State.DEVIS_RECAP
    save_conv(phone, conv)
    send_whatsapp(phone_full, "\n".join(lines))


def _generate_devis(phone: str, phone_full: str, conv: Dict):
    """Génère le devis PDF via l'API interne"""
    data = conv.get("data", {})
    send_whatsapp(phone_full, "⏳ *Génération du devis en cours...*")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)
        return
    
    try:
        # Préparer les données pour l'API
        tva_taux = float(entreprise.get("tva_taux", 20) or 20)
        
        prestations_for_api = []
        for p in data.get("prestations", []):
            prestations_for_api.append(Prestation(
                description=p.get("description", ""),
                quantite=float(p.get("quantite", 1)),
                unite=p.get("unite", "u"),
                prix_unitaire=float(p.get("prix_unitaire", 0)),
                tva_taux=tva_taux,
            ))
        
        # Construire la requête
        entreprise_model = Entreprise(
            nom=entreprise.get("nom", ""),
            gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""),
            adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""),
            tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""),
            logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux,
            mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            conditions_paiement=entreprise.get("conditions_paiement", "30% à la commande, solde à réception"),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        
        client_model = Client(
            nom=data.get("client_nom", ""),
            adresse=data.get("client_adresse", ""),
            tel=data.get("client_tel", ""),
            email=data.get("client_email", ""),
        )
        
        # Créer le devis dans le dashboard d'abord pour obtenir le numéro
        prestations_for_db = []
        for p in data.get("prestations", []):
            prestations_for_db.append({
                "description": p.get("description", ""),
                "quantite": p.get("quantite", 1),
                "unite": p.get("unite", "u"),
                "prix_unitaire_ht": p.get("prix_unitaire", 0),
                "prix_unitaire": p.get("prix_unitaire", 0),
                "tva_taux": tva_taux,
            })
        
        # Calculer les totaux
        total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in data.get("prestations", []))
        remise_type = data.get("remise_type")
        remise_valeur = data.get("remise_valeur", 0)
        remise = 0
        if remise_type == "pourcentage" and remise_valeur > 0:
            remise = total_ht * (remise_valeur / 100)
        total_ht_final = total_ht - remise
        total_tva = total_ht_final * (tva_taux / 100)
        total_ttc = total_ht_final + total_tva
        
        # Sauvegarder dans le dashboard (obtient le numéro auto-incrémenté)
        saved = save_devis_to_dashboard(
            entreprise_id=entreprise["id"],
            numero_devis="TEMP",  # Sera mis à jour après
            client_nom=data.get("client_nom", ""),
            client_email=data.get("client_email"),
            client_telephone=data.get("client_tel"),
            titre_projet=data.get("titre_projet"),
            prestations=prestations_for_db,
            total_ht=total_ht_final,
            total_ttc=total_ttc,
            pdf_url=None,
            word_url=None,
            remise_type=remise_type,
            remise_value=remise_valeur,
            delai=data.get("delai"),
        )
        
        if not saved:
            send_whatsapp(phone_full, "❌ Erreur lors de la création du devis.\n\n_Tapez *menu* pour revenir_")
            reset_conv(phone)
            return
        
        # Utiliser le numéro auto-généré par le dashboard
        numero_devis = saved.get("numero_devis", f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}")
        devis_db_id = saved.get("id", "")
        
        # Générer le PDF
        devis_request = DevisRequest(
            entreprise=entreprise_model,
            client=client_model,
            prestations=prestations_for_api,
            tva_taux=tva_taux,
            conditions_paiement=entreprise.get("conditions_paiement", "30% à la commande, solde à réception"),
            delai_realisation=data.get("delai", "À définir"),
            validite_jours=int(entreprise.get("delai_validite", 30) or 30),
            remise_type=remise_type,
            remise_valeur=remise_valeur or 0,
            acompte_pourcentage=data.get("acompte_pourcentage", 0),
            numero_devis=numero_devis,
        )
        
        filepath_pdf, _, total_ht_calc, total_ttc_calc = generer_pdf_devis(devis_request, numero_devis_force=numero_devis)
        
        # Upload
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis}.pdf")
        
        # Word
        filepath_word, _, _, _ = generer_word_devis(devis_request, numero_devis_force=numero_devis)
        word_url = upload_to_supabase(filepath_word, f"{numero_devis}.docx")
        
        # Mettre à jour le devis en base avec les URLs
        if supabase_client and devis_db_id:
            try:
                supabase_client.table("devis").update({
                    "numero_devis": numero_devis,
                    "pdf_url": pdf_url,
                    "word_url": word_url,
                    "total_ht": total_ht_calc,
                    "total_ttc": total_ttc_calc,
                }).eq("id", devis_db_id).execute()
            except Exception as e:
                logger.error(f"Erreur update devis: {e}")
        
        # Envoyer le PDF à l'utilisateur
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"📄 Devis {numero_devis}")
        
        # Message de succès avec actions
        send_whatsapp(phone_full, f"""✅ *Devis {numero_devis} créé !*

💰 Total : *{total_ttc_calc:.2f}€ TTC*

━━━━━━━━━━━━━━━━━━
*1.* 📱 Envoyer par WhatsApp
*2.* 📧 Envoyer par email
*3.* 📝 Nouveau devis
*4.* 💰 Créer facture d'acompte
*5.* 🏠 Menu""")
        
        # Sauvegarder l'état
        conv["state"] = State.DEVIS_GENERE
        conv["data"]["devis_genere"] = {
            "id": devis_db_id,
            "numero_devis": numero_devis,
            "client_nom": data.get("client_nom", ""),
            "client_tel": data.get("client_tel", ""),
            "client_email": data.get("client_email", ""),
            "total_ttc": total_ttc_calc,
            "total_ht": total_ht_calc,
            "pdf_url": pdf_url,
            "word_url": word_url,
            "titre_projet": data.get("titre_projet", ""),
        }
        save_conv(phone, conv)
        
    except Exception as e:
        logger.error(f"Erreur génération devis: {e}")
        import traceback
        traceback.print_exc()
        send_whatsapp(phone_full, f"❌ Erreur technique : {str(e)[:100]}\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)


def _generate_facture_acompte(phone: str, phone_full: str, conv: Dict, taux: float):
    """Génère une facture d'acompte"""
    data = conv.get("data", {})
    devis = data.get("selected_devis", {})
    
    send_whatsapp(phone_full, f"⏳ *Génération facture acompte {taux}%...*")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)
        return
    
    try:
        # Parser les prestations du devis
        prestations_raw = devis.get("prestations", "[]")
        if isinstance(prestations_raw, str):
            prestations_data = json.loads(prestations_raw)
        else:
            prestations_data = prestations_raw
        
        tva_taux = float(entreprise.get("tva_taux", 20) or 20)
        total_ht_devis = float(devis.get("total_ht", 0))
        total_ttc_devis = float(devis.get("total_ttc", 0))
        
        # Calculer l'acompte
        total_ht_acompte = round(total_ht_devis * taux / 100, 2)
        total_ttc_acompte = round(total_ttc_devis * taux / 100, 2)
        
        # Construire la prestation d'acompte
        prestations_api = [Prestation(
            description=f"Acompte {taux}% - {devis.get('titre_projet', devis.get('client_nom', ''))}",
            quantite=1,
            unite="forfait",
            prix_unitaire=total_ht_acompte,
            tva_taux=tva_taux,
        )]
        
        entreprise_model = Entreprise(
            nom=entreprise.get("nom", ""),
            gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""),
            adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""),
            tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""),
            logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux,
            mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        
        client_model = Client(
            nom=devis.get("client_nom", ""),
            adresse=devis.get("client_adresse", ""),
            tel=devis.get("telephone_client", ""),
            email=devis.get("client_email", ""),
        )
        
        facture_request = FactureRequest(
            entreprise=entreprise_model,
            client=client_model,
            prestations=prestations_api,
            tva_taux=tva_taux,
            numero_devis_origine=devis.get("numero_devis", ""),
            is_facture_acompte=True,
            taux_acompte=taux,
            total_ht=total_ht_acompte,
            total_ttc=total_ttc_acompte,
            total_ht_devis=total_ht_devis,
            total_ttc_devis=total_ttc_devis,
        )
        
        filepath_pdf, numero_facture, _, _ = generer_pdf_facture(facture_request)
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_facture}.pdf")
        
        filepath_word, _, _, _ = generer_word_facture(facture_request)
        word_url = upload_to_supabase(filepath_word, f"{numero_facture}.docx")
        
        # Sauvegarder dans le dashboard
        saved = save_facture_to_dashboard(
            entreprise_id=entreprise["id"],
            devis_id=devis.get("id"),
            numero_facture=numero_facture,
            client_nom=devis.get("client_nom", ""),
            client_email=devis.get("client_email"),
            client_telephone=devis.get("telephone_client"),
            client_adresse=devis.get("client_adresse"),
            titre_projet=devis.get("titre_projet"),
            prestations=[{"description": f"Acompte {taux}%", "quantite": 1, "unite": "forfait", "prix_unitaire": total_ht_acompte}],
            total_ht=total_ht_acompte,
            total_ttc=total_ttc_acompte,
            pdf_url=pdf_url,
            word_url=word_url,
            type_facture="acompte",
            tva_taux=tva_taux,
        )
        
        facture_id = saved.get("id", "") if saved else ""
        
        # Envoyer le PDF
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"🧾 Facture {numero_facture}")
        
        send_whatsapp(phone_full, f"""✅ *Facture d'acompte créée !*

🧾 {numero_facture}
💰 Acompte {taux}% : *{total_ttc_acompte:.2f}€ TTC*
📋 Devis : {devis.get('numero_devis', '')}

━━━━━━━━━━━━━━━━━━
*1.* 📱 Envoyer par WhatsApp
*2.* 📧 Envoyer par email
*3.* ✅ Marquer comme payée
*4.* 🏠 Menu""")
        
        conv["state"] = State.FACTURE_GENERE
        conv["data"]["facture_genere"] = {
            "id": facture_id,
            "numero_facture": numero_facture,
            "client_nom": devis.get("client_nom", ""),
            "client_tel": devis.get("telephone_client", ""),
            "client_email": devis.get("client_email", ""),
            "total_ttc": total_ttc_acompte,
            "pdf_url": pdf_url,
            "doc_type": "facture",
        }
        save_conv(phone, conv)
        
    except Exception as e:
        logger.error(f"Erreur génération facture acompte: {e}")
        import traceback
        traceback.print_exc()
        send_whatsapp(phone_full, f"❌ Erreur technique.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)


def _generate_facture_finale(phone: str, phone_full: str, conv: Dict):
    """Génère une facture finale (solde)"""
    data = conv.get("data", {})
    devis = data.get("selected_devis", {})
    
    send_whatsapp(phone_full, "⏳ *Génération facture finale...*")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)
        return
    
    try:
        tva_taux = float(entreprise.get("tva_taux", 20) or 20)
        
        # Récupérer les acomptes payés
        acompte_ttc_total = 0
        acompte_refs = []
        factures = devis.get("factures", [])
        for f in factures:
            if f.get("type_facture") == "acompte" and f.get("statut") == "payee":
                acompte_ttc_total += float(f.get("total_ttc", 0))
                acompte_refs.append(f.get("numero_facture", ""))
        
        # Parser les prestations du devis
        prestations_raw = devis.get("prestations", "[]")
        if isinstance(prestations_raw, str):
            prestations_data = json.loads(prestations_raw)
        else:
            prestations_data = prestations_raw
        
        prestations_api = []
        for p in prestations_data:
            prestations_api.append(Prestation(
                description=p.get("description", ""),
                quantite=float(p.get("quantite", 1)),
                unite=p.get("unite", "u"),
                prix_unitaire=float(p.get("prix_unitaire_ht", p.get("prix_unitaire", 0))),
                tva_taux=float(p.get("tva_taux", tva_taux)),
            ))
        
        entreprise_model = Entreprise(
            nom=entreprise.get("nom", ""),
            gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""),
            adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""),
            tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""),
            logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux,
            mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        
        client_model = Client(
            nom=devis.get("client_nom", ""),
            adresse=devis.get("client_adresse", ""),
            tel=devis.get("telephone_client", ""),
            email=devis.get("client_email", ""),
        )
        
        facture_request = FactureRequest(
            entreprise=entreprise_model,
            client=client_model,
            prestations=prestations_api,
            tva_taux=tva_taux,
            numero_devis_origine=devis.get("numero_devis", ""),
            acompte_ttc_deja_facture=acompte_ttc_total if acompte_ttc_total > 0 else None,
            acompte_references=acompte_refs if acompte_refs else None,
            remise_type=devis.get("remise_type"),
            remise_valeur=float(devis.get("remise_value", 0) or 0),
        )
        
        filepath_pdf, numero_facture, total_ht, total_ttc = generer_pdf_facture(facture_request)
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_facture}.pdf")
        
        filepath_word, _, _, _ = generer_word_facture(facture_request)
        word_url = upload_to_supabase(filepath_word, f"{numero_facture}.docx")
        
        reste_a_payer = total_ttc - acompte_ttc_total
        
        saved = save_facture_to_dashboard(
            entreprise_id=entreprise["id"],
            devis_id=devis.get("id"),
            numero_facture=numero_facture,
            client_nom=devis.get("client_nom", ""),
            client_email=devis.get("client_email"),
            client_telephone=devis.get("telephone_client"),
            client_adresse=devis.get("client_adresse"),
            titre_projet=devis.get("titre_projet"),
            prestations=prestations_data,
            total_ht=total_ht,
            total_ttc=total_ttc,
            pdf_url=pdf_url,
            word_url=word_url,
            type_facture="complete",
            remise_type=devis.get("remise_type"),
            remise_value=float(devis.get("remise_value", 0) or 0),
            tva_taux=tva_taux,
            solde_a_payer=reste_a_payer,
        )
        
        facture_id = saved.get("id", "") if saved else ""
        
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"🧾 Facture {numero_facture}")
        
        acompte_text = f"\n💰 Acompte déduit : -{acompte_ttc_total:.2f}€\n💰 *Reste à payer : {reste_a_payer:.2f}€*" if acompte_ttc_total > 0 else ""
        
        send_whatsapp(phone_full, f"""✅ *Facture finale créée !*

🧾 {numero_facture}
💰 Total TTC : {total_ttc:.2f}€{acompte_text}
📋 Devis : {devis.get('numero_devis', '')}

━━━━━━━━━━━━━━━━━━
*1.* 📱 Envoyer par WhatsApp
*2.* 📧 Envoyer par email
*3.* ✅ Marquer comme payée
*4.* 🏠 Menu""")
        
        conv["state"] = State.FACTURE_GENERE
        conv["data"]["facture_genere"] = {
            "id": facture_id,
            "numero_facture": numero_facture,
            "client_nom": devis.get("client_nom", ""),
            "client_tel": devis.get("telephone_client", ""),
            "client_email": devis.get("client_email", ""),
            "total_ttc": reste_a_payer if acompte_ttc_total > 0 else total_ttc,
            "pdf_url": pdf_url,
            "doc_type": "facture",
        }
        save_conv(phone, conv)
        
    except Exception as e:
        logger.error(f"Erreur génération facture finale: {e}")
        import traceback
        traceback.print_exc()
        send_whatsapp(phone_full, f"❌ Erreur technique.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)


def _send_email_action(phone: str, phone_full: str, conv: Dict, email: str, avec_signature: bool = False):
    """Envoie un email avec le document"""
    data = conv.get("data", {})
    send_doc = data.get("send_doc", {})
    doc_type = send_doc.get("doc_type", "devis")
    
    send_whatsapp(phone_full, f"📧 Envoi en cours à *{email}*...")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
        reset_conv(phone)
        return
    
    success = False
    if doc_type == "devis":
        success = send_email_devis(email, entreprise, send_doc, avec_signature=avec_signature)
    else:
        success = send_email_facture(email, entreprise, send_doc)
    
    if success:
        # Mettre à jour statut
        doc_id = send_doc.get("id", "")
        if doc_id:
            table = "devis" if doc_type == "devis" else "factures"
            update_document_status(table, doc_id, "envoye")
        
        signature_txt = " (avec signature)" if avec_signature else ""
        send_whatsapp(phone_full, f"✅ *Email envoyé à {email}*{signature_txt} !\n\n_Tapez *menu* pour revenir_")
    else:
        send_whatsapp(phone_full, f"❌ Erreur d'envoi email. Vérifiez l'adresse.\n\n_Tapez *menu* pour revenir_")
    
    reset_conv(phone)


# =============================================================================
# WEBHOOK ENDPOINT
# =============================================================================

@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(""),
    Body: str = Form(""),
    MediaUrl0: Optional[str] = Form(None),
    MediaContentType0: Optional[str] = Form(None),
    ProfileName: Optional[str] = Form(None),
    NumMedia: Optional[str] = Form("0"),
    MessageSid: Optional[str] = Form(None),
    SmsMessageSid: Optional[str] = Form(None),
    ButtonPayload: Optional[str] = Form(None),
    ButtonText: Optional[str] = Form(None),
):
    """Webhook WhatsApp Twilio"""
    try:
        # Anti-doublon
        msg_sid = MessageSid or SmsMessageSid or ""
        if msg_sid:
            now = datetime.now()
            if msg_sid in _processed_sids:
                return {"status": "duplicate"}
            _processed_sids[msg_sid] = now
            # Cleanup vieux SIDs (>5min)
            old = [s for s, t in _processed_sids.items() if (now - t).total_seconds() > 300]
            for s in old:
                del _processed_sids[s]
        
        phone = From.replace("whatsapp:", "").replace("+", "").strip()
        message = Body.strip()
        button = ButtonPayload or ButtonText or None
        
        logger.info(f"Webhook: phone={phone} msg='{message[:50]}' button={button} media={MediaUrl0}")
        
        handle_message(
            phone=phone,
            message=message,
            media_url=MediaUrl0,
            media_type=MediaContentType0,
            button_payload=button,
        )
        
        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "detail": str(e)[:100]}


# Endpoint debug sessions (optionnel, garder pour le dev)
@router.get("/api/whatsapp/sessions")
async def get_sessions():
    """Debug: voir les conversations actives"""
    return {
        "total": len(_conversations),
        "sessions": {
            phone: {"state": c.get("state"), "last_activity": c.get("last_activity")}
            for phone, c in _conversations.items()
        }
    }


# End of whatsapp_handler.py
