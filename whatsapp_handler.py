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
    DEVIS_CLIENT_SELECT = "devis_client_select"  # Auto-complétion client
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
    # Combo post-devis
    COMBO_CONFIRM = "combo_confirm"
    # Facture
    FACTURE_LISTE = "facture_liste"
    FACTURE_TYPE = "facture_type"
    FACTURE_ACOMPTE_TAUX = "facture_acompte_taux"
    FACTURE_GENERE = "facture_genere"
    # Duplication
    DEVIS_DUPLICATE_LISTE = "devis_duplicate_liste"
    DEVIS_DUPLICATE_CLIENT = "devis_duplicate_client"
    # Relances
    RELANCE_LISTE = "relance_liste"
    RELANCE_ACTION = "relance_action"
    RELANCE_MSG = "relance_msg"
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
        send_whatsapp(to, "👋 *Bienvenue sur Vocario !*\n\nTapez:\n*1* → 📝 Nouveau devis\n*2* → 📂 Mes documents\n*3* → ❓ Aide")
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
            send_whatsapp(to, "👋 *Bienvenue sur Vocario !*\n\nTapez:\n*1* → 📝 Nouveau devis\n*2* → 📂 Mes documents\n*3* → ❓ Aide")
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


# ==================== GESTION DES PLANS ====================

FREE_DEVIS_LIMIT = 5  # Devis par mois en plan Free

def get_user_plan(entreprise: Dict) -> str:
    """Retourne le plan de l'utilisateur : 'free' ou 'business'"""
    plan = (entreprise.get("plan") or entreprise.get("subscription") or "free").lower().strip()
    if plan in ["business", "pro", "premium", "paid"]:
        return "business"
    return "free"


def count_devis_this_month(entreprise_id: str) -> int:
    """Compte les devis créés ce mois-ci"""
    if not supabase_client:
        return 0
    try:
        now = datetime.now()
        first_of_month = now.strftime("%Y-%m-01")
        result = supabase_client.table("devis")\
            .select("id", count="exact")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .gte("created_at", first_of_month)\
            .execute()
        return result.count if result.count else len(result.data or [])
    except Exception as e:
        logger.error(f"Erreur count_devis_this_month: {e}")
        return 0


def check_can_create_devis(entreprise: Dict) -> tuple:
    """Vérifie si l'utilisateur peut créer un devis. Retourne (ok, message, remaining)"""
    plan = get_user_plan(entreprise)
    if plan == "business":
        return True, "", -1
    
    count = count_devis_this_month(entreprise["id"])
    remaining = FREE_DEVIS_LIMIT - count
    
    if remaining <= 0:
        return False, f"📊 Vous avez atteint la limite de *{FREE_DEVIS_LIMIT} devis/mois* du plan gratuit.\n\n🚀 Passez à *Vocario Business* (15€ HT/mois) pour des devis et factures illimités !\n\n👉 Rendez-vous sur *vocario.fr* pour upgrader.\n\n_Tapez *menu* pour revenir_", 0
    
    return True, "", remaining


def is_business(entreprise: Dict) -> bool:
    """Vérifie si l'utilisateur a le plan Business"""
    return get_user_plan(entreprise) == "business"


UPGRADE_MSG_FACTURES = "🔒 Les *factures* sont réservées au plan *Vocario Business* (15€ HT/mois).\n\n✅ Devis & factures illimités\n✅ Signature électronique\n✅ Factures d'acompte\n✅ Relances automatiques\n✅ Export PDF + Word\n\n👉 Rendez-vous sur *vocario.fr* pour upgrader.\n\n_Tapez *menu* pour revenir_"

UPGRADE_MSG_RELANCES = "🔒 Les *relances clients* sont réservées au plan *Vocario Business*.\n\n👉 *vocario.fr* pour upgrader.\n\n_Tapez *menu* pour revenir_"


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
# FONCTIONS BUSINESS : Dashboard, Clients, Prestations, Relances, Duplication
# =============================================================================

def get_activity_dashboard(entreprise_id: str) -> Dict:
    """Récupère les stats d'activité pour le menu intelligent"""
    stats = {"devis_en_attente": 0, "factures_impayees": 0, "montant_impaye": 0, "ca_mois": 0, "overdue_count": 0}
    if not supabase_client:
        return stats
    try:
        # Devis en attente (envoyés mais pas signés/acceptés)
        devis = supabase_client.table("devis")\
            .select("id, statut, total_ttc")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["en_attente", "envoye"])\
            .execute()
        stats["devis_en_attente"] = len(devis.data or [])
        
        # Factures impayées
        factures = supabase_client.table("factures")\
            .select("id, statut, total_ttc, date, created_at")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["en_attente", "envoyee"])\
            .execute()
        facs_impayees = factures.data or []
        stats["factures_impayees"] = len(facs_impayees)
        stats["montant_impaye"] = sum(f.get("total_ttc", 0) or 0 for f in facs_impayees)
        
        # Compter les factures en retard (> 30 jours)
        now = datetime.now()
        for f in facs_impayees:
            date_str = f.get("date") or f.get("created_at", "")
            try:
                if "T" in str(date_str):
                    fac_date = datetime.fromisoformat(date_str.replace("Z", ""))
                else:
                    fac_date = datetime.strptime(str(date_str), "%Y-%m-%d")
                if (now - fac_date).days > 30:
                    stats["overdue_count"] += 1
            except:
                pass
        
        # CA du mois (factures payées ce mois)
        first_of_month = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S")
        payees = supabase_client.table("factures")\
            .select("total_ttc")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .eq("statut", "payee")\
            .gte("created_at", first_of_month)\
            .execute()
        stats["ca_mois"] = sum(f.get("total_ttc", 0) or 0 for f in (payees.data or []))
        
    except Exception as e:
        logger.error(f"Erreur get_activity_dashboard: {e}")
    return stats


def get_recent_clients(entreprise_id: str, limit: int = 5) -> List[Dict]:
    """Récupère les clients uniques des devis récents"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("client_nom, client_email, telephone_client, client_adresse")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(30)\
            .execute()
        
        # Dédupliquer par nom (garder le plus récent)
        seen = set()
        clients = []
        for d in (result.data or []):
            nom = (d.get("client_nom") or "").strip()
            if nom and nom.lower() not in seen:
                seen.add(nom.lower())
                clients.append({
                    "nom": nom,
                    "email": d.get("client_email", "") or "",
                    "tel": d.get("telephone_client", "") or "",
                    "adresse": d.get("client_adresse", "") or "",
                })
                if len(clients) >= limit:
                    break
        return clients
    except Exception as e:
        logger.error(f"Erreur get_recent_clients: {e}")
        return []


def get_frequent_prestations(entreprise_id: str, limit: int = 5) -> List[Dict]:
    """Récupère les prestations les plus fréquentes"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("prestations")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()
        
        # Compter les prestations par description + prix
        presta_count = {}  # key = "description|prix" -> {count, data}
        for d in (result.data or []):
            prestations_raw = d.get("prestations")
            if not prestations_raw:
                continue
            try:
                if isinstance(prestations_raw, str):
                    prestations = json.loads(prestations_raw)
                else:
                    prestations = prestations_raw
                for p in prestations:
                    desc = (p.get("description") or "").strip()
                    prix = float(p.get("prix_unitaire") or p.get("prix_unitaire_ht") or 0)
                    unite = p.get("unite", "u") or "u"
                    if desc and prix > 0:
                        key = f"{desc.lower()}|{prix}|{unite}"
                        if key not in presta_count:
                            presta_count[key] = {"count": 0, "description": desc, "prix_unitaire": prix, "unite": unite}
                        presta_count[key]["count"] += 1
            except:
                continue
        
        # Trier par fréquence et prendre les top
        sorted_prestas = sorted(presta_count.values(), key=lambda x: x["count"], reverse=True)
        return sorted_prestas[:limit]
    except Exception as e:
        logger.error(f"Erreur get_frequent_prestations: {e}")
        return []


def get_overdue_documents(entreprise_id: str) -> List[Dict]:
    """Récupère les documents en retard (factures impayées > 15j, devis non signés > 7j)"""
    items = []
    if not supabase_client:
        return items
    try:
        now = datetime.now()
        
        # Factures impayées
        facs = supabase_client.table("factures")\
            .select("id, numero_facture, client_nom, total_ttc, date, created_at, statut, telephone_client, client_email")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["en_attente", "envoyee"])\
            .execute()
        
        for f in (facs.data or []):
            date_str = f.get("date") or f.get("created_at", "")
            try:
                if "T" in str(date_str):
                    doc_date = datetime.fromisoformat(date_str.replace("Z", ""))
                else:
                    doc_date = datetime.strptime(str(date_str), "%Y-%m-%d")
                days = (now - doc_date).days
                if days >= 15:
                    items.append({
                        "type": "facture",
                        "id": f.get("id"),
                        "numero": f.get("numero_facture", ""),
                        "client_nom": f.get("client_nom", ""),
                        "total_ttc": f.get("total_ttc", 0),
                        "days_overdue": days,
                        "tel": f.get("telephone_client", ""),
                        "email": f.get("client_email", ""),
                        "urgency": "red" if days > 30 else "yellow"
                    })
            except:
                pass
        
        # Devis envoyés non signés > 7 jours
        devis = supabase_client.table("devis")\
            .select("id, numero_devis, client_nom, total_ttc, date, created_at, statut, telephone_client, client_email")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["envoye"])\
            .execute()
        
        for d in (devis.data or []):
            date_str = d.get("date") or d.get("created_at", "")
            try:
                if "T" in str(date_str):
                    doc_date = datetime.fromisoformat(date_str.replace("Z", ""))
                else:
                    doc_date = datetime.strptime(str(date_str), "%Y-%m-%d")
                days = (now - doc_date).days
                if days >= 7:
                    items.append({
                        "type": "devis",
                        "id": d.get("id"),
                        "numero": d.get("numero_devis", ""),
                        "client_nom": d.get("client_nom", ""),
                        "total_ttc": d.get("total_ttc", 0),
                        "days_overdue": days,
                        "tel": d.get("telephone_client", ""),
                        "email": d.get("client_email", ""),
                        "urgency": "yellow"
                    })
            except:
                pass
        
        # Trier par urgence (factures impayées d'abord, puis par jours)
        items.sort(key=lambda x: (-1 if x["type"] == "facture" else 0, -x["days_overdue"]))
        return items[:10]
    except Exception as e:
        logger.error(f"Erreur get_overdue_documents: {e}")
        return []


def get_recent_devis_for_duplicate(entreprise_id: str, limit: int = 5) -> List[Dict]:
    """Récupère les devis récents pour duplication"""
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("id, numero_devis, client_nom, total_ttc, prestations, titre_projet, client_email, telephone_client, client_adresse, remise_type, remise_value")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Erreur get_recent_devis_for_duplicate: {e}")
        return []


UPGRADE_LINK = "vocario.fr/upgrade"

UPGRADE_MSG_DEVIS_LIMIT = """📊 *Limite atteinte*

Vous avez utilisé vos *5 devis gratuits* ce mois-ci.
Vos devis se réinitialisent le 1er du mois prochain.

🚀 *Débloquez tout maintenant :*

Vocario Business = *15€ HT/mois*
→ Devis & factures *illimités*
→ Signature électronique légale
→ Relances automatiques
→ Export Word + PDF

💡 _Un seul devis signé rembourse 1 an d'abonnement !_

👉 Tapez *upgrade* ou allez sur *vocario.fr/upgrade*

_Tapez *menu* pour revenir_"""

UPGRADE_MSG_CONTEXTUAL_FACTURE = """🔒 *Factures — Plan Business*

Avec Business, vous pourriez :
• Transformer ce devis en facture d'acompte en *10 secondes*
• Envoyer la facture par *email avec signature*
• *Relancer automatiquement* si impayée
• Suivre vos *paiements en temps réel*

Tout ça pour *15€ HT/mois* (18€ TTC)

💡 _Un seul devis signé rembourse 1 an d'abonnement !_

👉 Tapez *upgrade* ou allez sur *vocario.fr/upgrade*

_Tapez *menu* pour revenir_"""


# =============================================================================
# PARSING PRESTATIONS - REGEX LOCAL (rapide, pas d'API)
# =============================================================================

def parse_prestations_regex(texte: str) -> List[Dict]:
    """Parse prestations avec regex — couvre 80% des cas simples, 0 latence"""
    prestations = []
    
    # Normaliser le texte
    texte_clean = texte.replace("€", " €").replace("  ", " ").strip()
    
    # Séparer par lignes OU par "+" ou "et" en début de ligne
    lines = re.split(r'\n|(?:^|\s)\+\s', texte_clean)
    
    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue
        
        # Pattern 1: "Carrelage 30m2 50€" ou "Carrelage 30 m² à 50€" ou "Carrelage 30m2 x 50€"
        m = re.match(
            r'(.+?)\s+(\d+[.,]?\d*)\s*(m2|m²|ml|m|h|u|jours?|kg|l)\s*(?:[xX×àa@]\s*)?(\d+[.,]?\d*)\s*€?',
            line, re.IGNORECASE
        )
        if m:
            desc = m.group(1).strip().rstrip('-–—:').strip()
            qte = float(m.group(2).replace(',', '.'))
            unite = m.group(3).lower().replace('m2', 'm²').rstrip('s')
            prix = float(m.group(4).replace(',', '.'))
            if desc and prix > 0:
                prestations.append({"description": desc.capitalize(), "quantite": qte, "unite": unite, "prix_unitaire": prix})
                continue
        
        # Pattern 2: "Peinture forfait 800€" ou "Peinture 800€"
        m = re.match(
            r'(.+?)\s+(?:forfait\s+)?(\d+[.,]?\d*)\s*€',
            line, re.IGNORECASE
        )
        if m:
            desc = m.group(1).strip().rstrip('-–—:').strip()
            prix = float(m.group(2).replace(',', '.'))
            # Vérifier que desc n'est pas juste un nombre
            if desc and not desc.replace(' ', '').isdigit() and prix > 0:
                prestations.append({"description": desc.capitalize(), "quantite": 1, "unite": "forfait", "prix_unitaire": prix})
                continue
        
        # Pattern 3: "800€ peinture" ou "800 euros peinture salon"
        m = re.match(
            r'(\d+[.,]?\d*)\s*(?:€|euros?)\s+(.+)',
            line, re.IGNORECASE
        )
        if m:
            prix = float(m.group(1).replace(',', '.'))
            desc = m.group(2).strip()
            if desc and prix > 0:
                prestations.append({"description": desc.capitalize(), "quantite": 1, "unite": "forfait", "prix_unitaire": prix})
                continue
    
    # Si aucune ligne n'a matché, essayer le texte entier comme une seule prestation
    if not prestations:
        for pattern_fn in [
            # "carrelage 30m2 50€"
            lambda t: re.match(r'(.+?)\s+(\d+[.,]?\d*)\s*(m2|m²|ml|m|h|u|jours?|kg|l)\s*(?:[xX×àa@]\s*)?(\d+[.,]?\d*)\s*€?', t, re.IGNORECASE),
            # "peinture 800€"
            lambda t: re.match(r'(.+?)\s+(?:forfait\s+)?(\d+[.,]?\d*)\s*€', t, re.IGNORECASE),
        ]:
            m = pattern_fn(texte_clean)
            if m:
                groups = m.groups()
                if len(groups) == 4:
                    prestations.append({"description": groups[0].strip().capitalize(), "quantite": float(groups[1].replace(',','.')), "unite": groups[2].lower().replace('m2','m²'), "prix_unitaire": float(groups[3].replace(',','.'))})
                elif len(groups) == 2:
                    desc = groups[0].strip()
                    if desc and not desc.replace(' ','').isdigit():
                        prestations.append({"description": desc.capitalize(), "quantite": 1, "unite": "forfait", "prix_unitaire": float(groups[1].replace(',','.'))})
                break
    
    return prestations


def parse_express_devis(texte: str) -> Optional[Dict]:
    """
    Détecte et parse un devis express en un seul message.
    Format: "Dupont 0612345678 carrelage 30m2 50€"
    Retourne dict {client_nom, client_tel, prestations} ou None
    """
    # Chercher un numéro de téléphone dans le message
    phone_match = re.search(r'(0\d[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})', texte)
    # Chercher un prix
    price_match = re.search(r'\d+[.,]?\d*\s*€', texte)
    
    if not phone_match or not price_match:
        return None
    
    tel = re.sub(r'[^0-9]', '', phone_match.group(1))
    if len(tel) < 10:
        return None
    
    # Tout ce qui est AVANT le téléphone = nom du client
    before_phone = texte[:phone_match.start()].strip()
    # Tout ce qui est APRÈS le téléphone = prestations
    after_phone = texte[phone_match.end():].strip()
    
    if not before_phone or not after_phone:
        return None
    
    # Parser les prestations de la partie après le téléphone
    prestations = parse_prestations_regex(after_phone)
    if not prestations:
        return None
    
    return {
        "client_nom": before_phone.strip().title(),
        "client_tel": tel,
        "prestations": prestations,
    }


# =============================================================================
# IA - PARSING PRESTATIONS (Claude Haiku - fallback)
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
            
            # Ligne devis : numéro + type + projet + montant + statut
            label = projet if projet else d.get("numero_devis", "Devis")
            lines.append(f"*{idx}.* 📋 Devis · {label} · {total:.0f}€ {statut_emoji}")
            
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


def format_doc_detail(doc_type: str, doc: Dict, devis_parent: Dict = None, user_plan: str = "business") -> tuple:
    """Formate le détail d'un document avec actions. Retourne (texte, facture_index)"""
    lines = []
    facture_index = {}  # numéro -> facture data (pour navigation)
    is_free = (user_plan != "business")
    
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
        
        # Factures liées - numérotées et cliquables
        factures = doc.get("factures", [])
        if factures:
            lines.append("")
            lines.append("📎 *Factures liées :*")
            fac_num = 7  # Les factures commencent à 7
            for f in factures:
                ft_emoji = "💰" if f.get("type_facture") == "acompte" else "🧾"
                ft_label = "Acompte" if f.get("type_facture") == "acompte" else "Facture"
                fs = format_statut(f.get("statut", ""))
                lines.append(f"  *{fac_num}.* {ft_emoji} {ft_label} {f.get('total_ttc', 0):.0f}€ · {fs}")
                facture_index[str(fac_num)] = f
                fac_num += 1
        
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("*1.* 📱 Envoyer par WhatsApp")
        if is_free:
            lines.append("*2.* 📧 Envoyer par email 🔒")
            lines.append("*3.* 💰 Facture d'acompte 🔒")
            lines.append("*4.* 🧾 Facture finale 🔒")
        else:
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
    
    return "\n".join(lines), facture_index


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
        send_whatsapp(phone_full, "🎤 _Transcription en cours..._")
        transcribed = transcribe_audio(media_url)
        if transcribed:
            msg = transcribed
            msg_lower = msg.lower()
            send_whatsapp(phone_full, f"🎤 _\"{msg}\"_")
        else:
            send_whatsapp(phone_full, "⚠️ Impossible de comprendre le vocal.\n\n_Réessayez en parlant plus fort, ou écrivez votre message._")
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
        # Dashboard intelligent avant le menu template
        entreprise = get_entreprise(phone)
        if entreprise:
            business = is_business(entreprise)
            if business:
                # ── BUSINESS : Dashboard activité ──
                stats = get_activity_dashboard(entreprise["id"])
                dashboard_lines = ["📊 *Votre activité*\n"]
                if stats["devis_en_attente"] > 0:
                    dashboard_lines.append(f"📝 {stats['devis_en_attente']} devis en attente")
                if stats["factures_impayees"] > 0:
                    dashboard_lines.append(f"🔴 {stats['factures_impayees']} facture(s) impayée(s) — {stats['montant_impaye']:.0f}€")
                if stats["overdue_count"] > 0:
                    dashboard_lines.append(f"⚠️ {stats['overdue_count']} en retard > 30j")
                if stats["ca_mois"] > 0:
                    dashboard_lines.append(f"💰 CA du mois : {stats['ca_mois']:.0f}€")
                if len(dashboard_lines) > 1:
                    dashboard_lines.append("")
                    dashboard_lines.append("*4.* 📋 Dupliquer un devis")
                    dashboard_lines.append("*5.* 🔔 Relances clients")
                    send_whatsapp(phone_full, "\n".join(dashboard_lines))
            else:
                # ── FREE : Compteur devis ──
                _, limit_msg, remaining = check_can_create_devis(entreprise)
                used = 5 - remaining
                bar = "█" * used + "░" * remaining
                counter_msg = f"📊 Devis ce mois : *{used}/5* {bar}"
                if remaining <= 1 and remaining > 0:
                    counter_msg += f"\n⚠️ Plus qu'{remaining} devis gratuit !"
                elif remaining == 0:
                    counter_msg += "\n🔒 Limite atteinte — tapez *upgrade*"
                send_whatsapp(phone_full, counter_msg)
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    if msg_lower in ["annuler", "cancel", "stop"]:
        reset_conv(phone)
        send_whatsapp(phone_full, "❌ Annulé.\n\n_Tapez *menu* pour recommencer._")
        return
    
    # Raccourci global "upgrade"
    if msg_lower in ["upgrade", "business", "passer business", "passer pro", "abonnement"]:
        send_whatsapp(phone_full, f"""🚀 *Vocario Business* — 15€ HT/mois

✅ Devis & factures *illimités*
✅ Signature électronique légale
✅ Factures d'acompte en 1 clic
✅ Relances clients automatiques
✅ Export Word + PDF professionnel
✅ Logo & couleurs personnalisés
✅ Tableau de bord & statistiques
✅ Support prioritaire

💡 _Un seul devis signé rembourse 1 an d'abonnement !_

👉 *{UPGRADE_LINK}*

_Tapez *menu* pour revenir_""")
        return
    
    # ── Raccourcis globaux : boutons template fonctionnent depuis n'importe quel écran ──
    # On reset et redirige vers le MENU qui gère la logique
    
    if state != State.MENU:
        is_global_shortcut = False
        
        if button_payload in ["nouveau_devis", "new_devis", "Nouveau devis"]:
            is_global_shortcut = True
        elif button_payload in ["mes_documents", "documents", "Mes documents"]:
            is_global_shortcut = True
        elif button_payload in ["aide", "help", "Aide"]:
            is_global_shortcut = True
        elif msg_lower in ["nouveau devis", "créer devis", "mes documents", "documents", "mes docs", "docs", "aide", "help"]:
            is_global_shortcut = True
        
        if is_global_shortcut:
            reset_conv(phone)
            conv = get_conv(phone)
            conv["state"] = State.MENU
            save_conv(phone, conv)
            # Relancer handle_message depuis l'état MENU
            handle_message(phone, message, button_payload=button_payload)
            return
    
    if msg_lower == "retour":
        retour_map = {
            State.DEVIS_TEL: State.DEVIS_NOM,
            State.DEVIS_PRESTATIONS: State.DEVIS_TEL,
            State.DEVIS_RECAP: State.DEVIS_PRESTATIONS,
            # Depuis enrichissement récap → retour au récap
            State.DEVIS_EMAIL: State.DEVIS_RECAP,
            State.DEVIS_ADRESSE: State.DEVIS_RECAP,
            State.DEVIS_PROJET: State.DEVIS_RECAP,
            State.DEVIS_REMISE: State.DEVIS_RECAP,
            State.DEVIS_ACOMPTE: State.DEVIS_RECAP,
            State.DEVIS_DELAI: State.DEVIS_RECAP,
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
            # Vérifier la limite du plan Free
            entreprise = get_entreprise(phone)
            if entreprise:
                can_create, limit_msg, remaining = check_can_create_devis(entreprise)
                if not can_create:
                    # Nudge progressif : message contextuel avec argument chiffré
                    send_whatsapp(phone_full, UPGRADE_MSG_DEVIS_LIMIT)
                    return
                
                # Nudge à 4/5 (1 restant)
                if remaining == 1:
                    send_whatsapp(phone_full, f"⚠️ _Dernier devis gratuit ce mois ! Tapez *upgrade* pour passer en illimité._")
                
                # Auto-complétion : proposer les clients récents (Business uniquement)
                if is_business(entreprise):
                    clients = get_recent_clients(entreprise["id"])
                    if clients:
                        conv["state"] = State.DEVIS_CLIENT_SELECT
                        conv["data"] = {"recent_clients": clients}
                        save_conv(phone, conv)
                        lines = ["📝 *NOUVEAU DEVIS*\n", "━━━━━━━━━━━━━━━━━━", "👤 *Client récent ou nouveau ?*\n"]
                        for i, c in enumerate(clients, 1):
                            label = f"*{i}.* {c['nom']}"
                            if c.get("tel"):
                                label += f" ({c['tel'][-4:]})"
                            lines.append(label)
                        lines.append(f"\n*{len(clients)+1}.* ➕ Nouveau client")
                        lines.append("\n_Tapez le numéro ou directement le nom_")
                        send_whatsapp(phone_full, "\n".join(lines))
                        return
            
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            send_whatsapp(phone_full, """📝 *NOUVEAU DEVIS*

👤 Entrez le *nom du client*

⚡ *Devis express :* envoyez tout en 1 message !
→ _Dupont 0612345678 carrelage 30m² 50€_""")
            return
        
        if button_payload in ["mes_documents", "documents", "Mes documents"] or msg_lower in ["2", "documents", "mes documents", "docs", "mes docs"]:
            _show_documents(phone, phone_full, conv)
            return
        
        # "facture" en texte libre → rediriger vers documents avec indice
        if msg_lower in ["facture", "nouvelle facture", "créer facture"]:
            send_whatsapp(phone_full, "🧾 Pour créer une facture, ouvrez un devis depuis *Mes documents* et choisissez *Facturer*.\n\n_Ouverture de vos documents..._")
            _show_documents(phone, phone_full, conv)
            return
        
        if button_payload in ["aide", "help", "Aide"] or msg_lower in ["3", "aide", "help"]:
            aide_msg = """❓ *AIDE VOCARIO*

📝 *Créer un devis*
Tapez *1* ou appuyez sur "Nouveau devis"

⚡ *Devis express* — Gagnez du temps !
Envoyez tout en 1 seul message :
→ _Dupont 0612345678 carrelage 30m² 50€_
Vocario crée le devis automatiquement.

📂 *Mes documents*
Tapez *2* pour retrouver vos devis et factures.
Depuis un devis, vous pouvez facturer, envoyer, relancer.

🎤 *Messages vocaux*
Envoyez un vocal, Vocario comprend !

🔄 *Navigation*
_*retour* → revenir en arrière_
_*menu* → revenir à l'accueil_

💬 *Support : contact@vocario.fr*"""
            send_whatsapp(phone_full, aide_msg)
            return
        
        # Option 4 : Dupliquer un devis (Business)
        if msg_lower in ["4", "dupliquer", "copier", "dupliquer devis"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
                return
            if not is_business(entreprise):
                send_whatsapp(phone_full, f"🔒 La *duplication de devis* est réservée au plan Business.\n\n👉 *{UPGRADE_LINK}*\n\n_Tapez *menu* pour revenir_")
                return
            devis_list = get_recent_devis_for_duplicate(entreprise["id"])
            if not devis_list:
                send_whatsapp(phone_full, "📭 Aucun devis à dupliquer.\n\n_Tapez *menu* pour revenir_")
                return
            lines = ["📋 *DUPLIQUER UN DEVIS*\n", "Choisissez le devis à copier :\n"]
            for i, d in enumerate(devis_list, 1):
                client = d.get("client_nom", "")
                total = d.get("total_ttc", 0)
                projet = d.get("titre_projet", "")
                label = f"*{i}.* {client} | {total:.0f}€"
                if projet:
                    label += f" | {projet[:20]}"
                lines.append(label)
            lines.append(f"\n_Tapez le numéro (1-{len(devis_list)})_")
            lines.append("_Tapez *menu* pour revenir_")
            conv["state"] = State.DEVIS_DUPLICATE_LISTE
            conv["data"] = {"duplicate_options": devis_list}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        # Option 5 : Relances (Business)
        if msg_lower in ["5", "relance", "relances", "relancer"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "❌ Entreprise non trouvée.\n\n_Tapez *menu* pour revenir_")
                return
            if not is_business(entreprise):
                send_whatsapp(phone_full, f"🔒 Les *relances clients* sont réservées au plan Business.\n\n👉 *{UPGRADE_LINK}*\n\n_Tapez *menu* pour revenir_")
                return
            overdue = get_overdue_documents(entreprise["id"])
            if not overdue:
                send_whatsapp(phone_full, "✅ *Rien à relancer !*\n\nTous vos documents sont à jour. 👏\n\n_Tapez *menu* pour revenir_")
                return
            lines = ["🔔 *RELANCES CLIENTS*\n"]
            for i, item in enumerate(overdue, 1):
                emoji = "🔴" if item["urgency"] == "red" else "🟡"
                type_label = "Facture" if item["type"] == "facture" else "Devis"
                lines.append(f"*{i}.* {emoji} {type_label} {item['numero']} | {item['client_nom']} | {item['total_ttc']:.0f}€ | {item['days_overdue']}j")
            lines.append(f"\n_Tapez le numéro (1-{len(overdue)}) pour relancer_")
            lines.append("_Tapez *menu* pour revenir_")
            conv["state"] = State.RELANCE_LISTE
            conv["data"] = {"relance_items": overdue}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        # Message libre depuis le menu → re-envoyer le menu
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    # =========================================================================
    # FLOW DEVIS - ÉTAPES
    # =========================================================================
    
    if state == State.DEVIS_CLIENT_SELECT:
        clients = data.get("recent_clients", [])
        # Nouveau client
        new_client_num = str(len(clients) + 1)
        if msg_lower in [new_client_num, "nouveau", "new", "autre"]:
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "👤 Entrez le *nom du client*\n\n⚡ *Devis express :* envoyez tout en 1 message !\n→ _Dupont 0612345678 carrelage 30m² 50€_")
            return
        # Sélection par numéro
        try:
            idx = int(msg) - 1
            if 0 <= idx < len(clients):
                selected = clients[idx]
                # Pré-remplir les données du client → sauter directement aux prestations
                conv["data"] = {
                    "client_nom": selected["nom"],
                    "client_tel": selected.get("tel", ""),
                    "client_email": selected.get("email", ""),
                    "client_adresse": selected.get("adresse", ""),
                }
                conv["state"] = State.DEVIS_PRESTATIONS
                save_conv(phone, conv)
                
                # Suggestions de prestations favorites (Business)
                favorites_msg = ""
                entreprise = get_entreprise(phone)
                if entreprise and is_business(entreprise):
                    favs = get_frequent_prestations(entreprise["id"])
                    if favs:
                        fav_lines = ["\n💡 *Vos prestations habituelles :*"]
                        for i, f in enumerate(favs[:3], 1):
                            fav_lines.append(f"*F{i}.* {f['description']} | {f['prix_unitaire']:.0f}€/{f['unite']}")
                        fav_lines.append("_Tapez F1, F2... pour les ajouter_")
                        favorites_msg = "\n".join(fav_lines)
                        conv["data"]["_favorites"] = favs[:3]
                        save_conv(phone, conv)
                
                send_whatsapp(phone_full, f"""✅ Client : *{selected['nom']}*
{('📞 ' + selected['tel']) if selected.get('tel') else ''}
{('📧 ' + selected['email']) if selected.get('email') else ''}

🔨 *Décrivez les travaux avec les prix :*

_Exemples :_
• _Carrelage 30m² 50€_
• _Peinture salon forfait 800€_

Envoyez tout en un message ou un vocal 🎤{favorites_msg}""")
                return
        except ValueError:
            pass
        # Texte libre = nouveau nom de client
        conv["data"] = {"client_nom": msg}
        conv["state"] = State.DEVIS_TEL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"""✅ Client : *{msg}*

📞 *Numéro de téléphone ?*

_Exemple: 06 12 34 56 78_""")
        return
    
    if state == State.DEVIS_NOM:
        if msg == "__show__":
            send_whatsapp(phone_full, "👤 Entrez le *nom du client*\n\n⚡ *Devis express :* envoyez tout en 1 message !\n→ _Dupont 0612345678 carrelage 30m² 50€_")
            return
        
        # Mode express : détecter nom + tél + prestations en un message
        express = parse_express_devis(msg)
        if express:
            data["client_nom"] = express["client_nom"]
            data["client_tel"] = express["client_tel"]
            data["prestations"] = express["prestations"]
            data["_from_express"] = True
            conv["data"] = data
            
            total_ht = sum(p["quantite"] * p["prix_unitaire"] for p in express["prestations"])
            presta_lines = []
            for p in express["prestations"]:
                t = p["quantite"] * p["prix_unitaire"]
                if p["quantite"] == 1 and p["unite"] in ["forfait", "u"]:
                    presta_lines.append(f"• {p['description']} = {t:.0f}€")
                else:
                    presta_lines.append(f"• {p['description']} {p['quantite']} {p['unite']} × {p['prix_unitaire']:.0f}€ = {t:.0f}€")
            
            send_whatsapp(phone_full, f"""⚡ *Devis express détecté !*

👤 {express['client_nom']}
📞 {express['client_tel']}
{chr(10).join(presta_lines)}

💰 *Total HT : {total_ht:.2f}€*""")
            
            # Aller directement au récap
            _show_recap(phone, phone_full, conv)
            return
        
        data["client_nom"] = msg
        conv["data"] = data
        conv["state"] = State.DEVIS_TEL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"""✅ Client : *{msg}*

📞 *Numéro de téléphone ?*

_Exemple: 06 12 34 56 78_
_Tapez *retour* pour modifier_""")
        return
    
    if state == State.DEVIS_TEL:
        if msg == "__show__":
            send_whatsapp(phone_full, f"👤 {data.get('client_nom', '')}\n\n📞 *Téléphone du client ?*\n\n_Exemple: 06 12 34 56 78_")
            return
        tel = re.sub(r'[^0-9+]', '', msg)
        if len(tel) < 10:
            send_whatsapp(phone_full, "❌ Numéro invalide (minimum 10 chiffres).\n\n_Exemple: 0612345678_")
            return
        data["client_tel"] = tel
        conv["data"] = data
        conv["state"] = State.DEVIS_PRESTATIONS
        save_conv(phone, conv)
        
        # Suggestions de prestations favorites (Business)
        favorites_msg = ""
        entreprise = get_entreprise(phone)
        if entreprise and is_business(entreprise):
            favs = get_frequent_prestations(entreprise["id"])
            if favs:
                fav_lines = ["\n💡 *Vos prestations habituelles :*"]
                for i, f in enumerate(favs[:3], 1):
                    fav_lines.append(f"*F{i}.* {f['description']} | {f['prix_unitaire']:.0f}€/{f['unite']}")
                fav_lines.append("_Tapez F1, F2... pour les ajouter_")
                favorites_msg = "\n".join(fav_lines)
                conv["data"]["_favorites"] = favs[:3]
                save_conv(phone, conv)
        
        send_whatsapp(phone_full, f"""✅ Tél : *{tel}*

🔨 *Décrivez les travaux avec les prix :*

_Exemples :_
• _Carrelage 30m² 50€_
• _Peinture salon forfait 800€_
• _Main d'œuvre 10h 45€_

Envoyez tout en un message ou un vocal 🎤{favorites_msg}""")
        return
    
    if state == State.DEVIS_EMAIL:
        if msg == "__show__":
            send_whatsapp(phone_full, "📧 *Email du client*\n\nQuel est son *email* ?\n_Tapez *non* si pas d'email_")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_email"] = ""
        elif "@" in msg and "." in msg:
            data["client_email"] = msg.lower().strip()
        else:
            send_whatsapp(phone_full, "⚠️ Ça ne ressemble pas à un email.\n\nExemple : *client@email.com*\nOu tapez *non* pour passer")
            return
        
        conv["data"] = data
        # Si on vient du récap, retourner au récap
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            email_txt = data["client_email"] or "Non renseigné"
            send_whatsapp(phone_full, f"✅ Email : *{email_txt}*")
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_ADRESSE
        save_conv(phone, conv)
        email_txt = data["client_email"] or "Non renseigné"
        send_whatsapp(phone_full, f"✅ Email : *{email_txt}*\n\n📍 *Adresse du chantier/client* ?\n\n_Tapez *non* si pas d'adresse_")
        return
    
    if state == State.DEVIS_ADRESSE:
        if msg == "__show__":
            send_whatsapp(phone_full, "📍 *Adresse du client*\n\nQuelle est l'*adresse* ?\n_Tapez *non* si pas d'adresse_")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_adresse"] = ""
        else:
            data["client_adresse"] = msg
        
        conv["data"] = data
        # Si on vient du récap, retourner au récap
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            addr_txt = data["client_adresse"] or "Non renseigné"
            send_whatsapp(phone_full, f"✅ Adresse : *{addr_txt}*")
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_PROJET
        save_conv(phone, conv)
        addr_txt = data["client_adresse"] or "Non renseigné"
        send_whatsapp(phone_full, f"✅ Adresse : *{addr_txt}*\n\n📁 Quel est le *nom du projet* ?\n\n_Exemple: Rénovation salle de bain_")
        return
    
    if state == State.DEVIS_PROJET:
        if msg == "__show__":
            send_whatsapp(phone_full, "📁 *Nom du projet*\n\nQuel est le *nom du projet* ?")
            return
        data["titre_projet"] = msg
        conv["data"] = data
        # Si on vient du récap, retourner au récap
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"✅ Projet : *{msg}*")
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_PRESTATIONS
        save_conv(phone, conv)
        
        # Suggestions de prestations favorites (Business)
        favorites_msg = ""
        entreprise = get_entreprise(phone)
        if entreprise and is_business(entreprise):
            favs = get_frequent_prestations(entreprise["id"])
            if favs:
                fav_lines = ["\n💡 *Vos prestations habituelles :*"]
                for i, f in enumerate(favs[:3], 1):
                    fav_lines.append(f"*F{i}.* {f['description']} | {f['prix_unitaire']:.0f}€/{f['unite']}")
                fav_lines.append("_Tapez F1, F2... pour les ajouter_")
                favorites_msg = "\n".join(fav_lines)
                conv["data"]["_favorites"] = favs[:3]
                save_conv(phone, conv)
        
        send_whatsapp(phone_full, f"""✅ Projet : *{msg}*

🔨 *Décrivez les travaux avec les prix* :

_Exemples :_
• _Carrelage 30m² 50€_
• _Peinture salon forfait 800€_
• _Main d'œuvre 10h 45€_

Envoyez tout en un message ou un vocal 🎤{favorites_msg}""")
        return
    
    if state == State.DEVIS_PRESTATIONS:
        if msg == "__show__":
            send_whatsapp(phone_full, "🔨 *Décrivez les travaux avec les prix*\n\n_Exemples :_\n• _Carrelage 30m² 50€_\n• _Peinture forfait 800€_\n\n_Envoyez tout en un message ou un vocal 🎤_")
            return
        
        # Raccourci favoris F1, F2, F3
        favs = data.get("_favorites", [])
        if msg_lower.startswith("f") and len(msg_lower) <= 3:
            try:
                fav_idx = int(msg_lower[1:]) - 1
                if 0 <= fav_idx < len(favs):
                    selected_fav = favs[fav_idx]
                    # Demander la quantité
                    send_whatsapp(phone_full, f"✅ *{selected_fav['description']}* — {selected_fav['prix_unitaire']:.0f}€/{selected_fav['unite']}\n\nQuelle *quantité* ? _(ex: 30)_")
                    data["_pending_fav"] = selected_fav
                    conv["data"] = data
                    save_conv(phone, conv)
                    return
            except (ValueError, IndexError):
                pass
        
        # Si on attend une quantité pour un favori
        pending_fav = data.get("_pending_fav")
        if pending_fav:
            try:
                qte = float(msg.replace(",", ".").strip())
                new_presta = {
                    "description": pending_fav["description"],
                    "quantite": qte,
                    "unite": pending_fav["unite"],
                    "prix_unitaire": pending_fav["prix_unitaire"]
                }
                existing = data.get("prestations", [])
                existing.append(new_presta)
                data["prestations"] = existing
                data.pop("_pending_fav", None)
                total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in existing)
                
                lines = ["✅ *Prestations :*\n"]
                for p in existing:
                    t = p["quantite"] * p["prix_unitaire"]
                    lines.append(f"• {p['description']} {p['quantite']} {p['unite']} × {p['prix_unitaire']:.0f}€ = {t:.0f}€")
                lines.append(f"\n💰 *Total HT : {total_ht:.2f}€*")
                lines.append("\n*1.* ➕ Ajouter une prestation")
                lines.append("*2.* ✅ Continuer")
                lines.append("*3.* 🔄 Refaire")
                
                conv["data"] = data
                conv["state"] = State.DEVIS_PRESTATIONS_SUITE
                save_conv(phone, conv)
                send_whatsapp(phone_full, "\n".join(lines))
                return
            except ValueError:
                data.pop("_pending_fav", None)
                conv["data"] = data
                save_conv(phone, conv)
                # Continue to normal parsing below
        
        # Parser les prestations : REGEX d'abord (instantané), IA en fallback
        prestations = parse_prestations_regex(msg)
        
        if not prestations:
            # Fallback: IA (plus lent mais comprend le langage naturel)
            send_whatsapp(phone_full, "⏳ Analyse en cours...")
            prestations = parse_prestations_ia(msg)
        
        if not prestations:
            send_whatsapp(phone_full, """❌ Je n'ai pas trouvé de *prix* dans votre message.

Essayez ce format :
• _Carrelage 30m² 50€_
• _Peinture salon 800€_
• _Main d'œuvre 10h 45€_

💡 _Le prix en € est obligatoire !_""")
            return
        
        # APPEND aux prestations existantes (si "Ajouter une prestation")
        existing = data.get("_prestations_precedentes", [])
        if existing:
            prestations = existing + prestations
            data.pop("_prestations_precedentes", None)  # Nettoyer le flag
        
        data["prestations"] = prestations
        
        # Calculer total HT sur TOUTES les prestations
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
            # Skip les options → aller directement au récap enrichi
            _show_recap(phone, phone_full, conv)
            return
        
        if msg_lower in ["3", "refaire"]:
            data.pop("_prestations_precedentes", None)
            data.pop("prestations", None)
            conv["data"] = data
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
                data["_from_recap"] = False
                conv["data"] = data
                conv["state"] = State.DEVIS_RECAP
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"✅ Remise *{remise}%* ajoutée !")
                _show_recap(phone, phone_full, conv)
                return
        except:
            pass
        send_whatsapp(phone_full, "⚠️ Entrez un pourcentage valide.\n\n_Exemple : *10* pour 10% de remise_")
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
                send_whatsapp(phone_full, "⚠️ Tapez *1* (30%), *2* (40%), *3* (50%) ou un pourcentage")
                return
        
        if 0 < acompte <= 100:
            data["acompte_pourcentage"] = acompte
            data["_from_recap"] = False
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"✅ Acompte *{acompte}%* ajouté !")
            _show_recap(phone, phone_full, conv)
            return
        send_whatsapp(phone_full, "⚠️ Pourcentage invalide (entre 1 et 100)")
        return
    
    if state == State.DEVIS_DELAI:
        data["delai"] = msg
        data["_from_recap"] = False
        conv["data"] = data
        conv["state"] = State.DEVIS_RECAP
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ Délai : *{msg}*")
        _show_recap(phone, phone_full, conv)
        return
    
    if state == State.DEVIS_RECAP:
        # Sub-state: attente d'input enrichissement
        adding = data.get("_recap_adding")
        if adding == "email":
            if "@" in msg and "." in msg:
                data["client_email"] = msg.lower().strip()
            elif msg_lower in ["non", "annuler", "retour"]:
                pass
            else:
                send_whatsapp(phone_full, "⚠️ Email invalide. Réessayez ou tapez *non*")
                return
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        if adding == "adresse":
            if msg_lower not in ["non", "annuler", "retour"]:
                data["client_adresse"] = msg
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        if adding == "projet":
            if msg_lower not in ["non", "annuler", "retour"]:
                data["titre_projet"] = msg
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        if adding == "remise":
            try:
                val = float(msg.replace("%", "").replace(",", ".").strip())
                if 0 < val <= 100:
                    data["remise_type"] = "pourcentage"
                    data["remise_valeur"] = val
            except ValueError:
                if msg_lower not in ["non", "annuler", "retour"]:
                    send_whatsapp(phone_full, "⚠️ Entrez un pourcentage valide (ex: 10)")
                    return
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        if adding == "acompte":
            acompte_map = {"1": 30, "2": 40, "3": 50}
            if msg_lower in acompte_map:
                data["acompte_pourcentage"] = acompte_map[msg_lower]
            else:
                try:
                    val = float(msg.replace("%", "").replace(",", ".").strip())
                    if 0 < val <= 100:
                        data["acompte_pourcentage"] = val
                except ValueError:
                    if msg_lower not in ["non", "annuler", "retour"]:
                        send_whatsapp(phone_full, "⚠️ Tapez *1* (30%), *2* (40%), *3* (50%) ou un autre %")
                        return
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        if adding == "delai":
            if msg_lower not in ["non", "annuler", "retour"]:
                data["delai"] = msg
            data.pop("_recap_adding", None)
            conv["data"] = data
            _show_recap(phone, phone_full, conv)
            return
        
        # Actions principales
        if msg_lower in ["1", "valider", "ok", "oui", "confirmer", "go"]:
            _generate_devis(phone, phone_full, conv)
            return
        if msg_lower in ["2", "modifier"]:
            conv["state"] = State.DEVIS_MODIFIER
            conv["data"]["_from_recap"] = True
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
        
        # Enrichissement inline
        if msg_lower == "3" and not data.get("client_email"):
            data["_recap_adding"] = "email"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "📧 *Email du client ?*\n\n_Tapez *non* pour annuler_")
            return
        if msg_lower == "4" and not data.get("client_adresse"):
            data["_recap_adding"] = "adresse"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "📍 *Adresse du chantier/client ?*\n\n_Tapez *non* pour annuler_")
            return
        if msg_lower == "5" and not data.get("titre_projet"):
            data["_recap_adding"] = "projet"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "🏗️ *Nom du projet ?*\n\n_Exemple: Rénovation salle de bain_")
            return
        if msg_lower == "6" and not data.get("remise_type"):
            data["_recap_adding"] = "remise"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "🏷️ *Pourcentage de remise ?*\n\n_Exemple: 10_")
            return
        if msg_lower == "7" and not data.get("acompte_pourcentage"):
            data["_recap_adding"] = "acompte"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 *Pourcentage d'acompte ?*\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n_Ou tapez un autre %_")
            return
        if msg_lower == "8" and not data.get("delai"):
            data["_recap_adding"] = "delai"
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, "⏱️ *Délai de réalisation ?*\n\n_Exemple: 2 semaines_")
            return
        
        if msg_lower in ["0", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Devis annulé.\n\n_Tapez *menu* pour recommencer._")
            return
        send_whatsapp(phone_full, "Tapez *1* (valider), *2* (modifier) ou *0* (annuler)")
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
        entreprise = get_entreprise(phone)
        user_is_business = entreprise and is_business(entreprise)
        
        # Option 1 : WhatsApp (tous plans)
        if msg_lower in ["1", "whatsapp", "envoyer"]:
            tel_client = devis_info.get("client_tel") or data.get("client_tel", "")
            if tel_client:
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = devis_info
                conv["data"]["send_doc"]["default_tel"] = tel_client
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"📱 *Envoi WhatsApp*\n\nClient : {devis_info.get('client_nom', '')}\nNuméro : *{tel_client}*\n\n*1.* ✅ Envoyer à ce numéro\n*2.* 📝 Autre numéro\n*3.* ❌ Annuler")
                return
            else:
                send_whatsapp(phone_full, "📱 Entrez le numéro du client :\n\n_Exemple: 0612345678_")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = devis_info
                save_conv(phone, conv)
                return
        
        # Business : 2=Email, 3=Acompte, 4=Nouveau, 5=Menu
        if user_is_business:
            if msg_lower in ["2", "email"]:
                email_client = devis_info.get("client_email") or data.get("client_email", "")
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                conv["data"]["send_doc"] = devis_info
                conv["data"]["send_doc"]["default_email"] = email_client
                conv["data"]["send_doc"]["doc_type"] = "devis"
                save_conv(phone, conv)
                if email_client:
                    send_whatsapp(phone_full, f"📧 *Envoi Email*\n\nClient : {devis_info.get('client_nom', '')}\nEmail : *{email_client}*\n\n*1.* ✍️ Avec signature électronique\n*2.* 📄 Sans signature (PDF seul)\n*3.* 📝 Autre email\n*4.* ❌ Annuler")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                    conv["state"] = State.DOCS_ENVOYER_EMAIL
                    save_conv(phone, conv)
                return
            
            if msg_lower in ["3", "acompte", "facture"]:
                conv["state"] = State.FACTURE_ACOMPTE_TAUX
                conv["data"]["selected_devis"] = devis_info
                save_conv(phone, conv)
                send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\nQuel pourcentage ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre")
                return
            
            if msg_lower in ["4", "nouveau", "nouveau devis"]:
                reset_conv(phone)
                conv = get_conv(phone)
                conv["state"] = State.DEVIS_NOM
                conv["data"] = {}
                save_conv(phone, conv)
                handle_message(phone, "__show__")
                return
            
            if msg_lower in ["5", "menu"]:
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
        
        # Free : 2=Nouveau, 3=Menu
        else:
            if msg_lower in ["2", "nouveau", "nouveau devis"]:
                reset_conv(phone)
                conv = get_conv(phone)
                conv["state"] = State.DEVIS_NOM
                conv["data"] = {}
                save_conv(phone, conv)
                handle_message(phone, "__show__")
                return
            
            if msg_lower in ["3", "menu"]:
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
            
            if msg_lower in ["email"]:
                send_whatsapp(phone_full, f"🔒 L'envoi par *email* est réservé au plan Business.\n\n👉 *{UPGRADE_LINK}*\n\n_Tapez *1* pour envoyer par WhatsApp_")
                return
            
            if msg_lower in ["acompte", "facture"]:
                send_whatsapp(phone_full, f"🔒 Les *factures* sont réservées au plan Business.\n\n👉 *{UPGRADE_LINK}*\n\n_Tapez *1* pour envoyer par WhatsApp_")
                return
        
        if msg_lower in ["menu"]:
            reset_conv(phone)
            send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
            return
        
        send_whatsapp(phone_full, "Tapez un numéro pour choisir une option")
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
            _show_documents(phone, phone_full, conv)
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
            
            # Récupérer le plan pour adapter l'affichage
            entreprise = get_entreprise(phone)
            plan = get_user_plan(entreprise) if entreprise else "free"
            detail_text, facture_index = format_doc_detail(doc_entry["type"], doc_entry["data"], doc_entry.get("devis"), user_plan=plan)
            data["facture_index"] = facture_index  # Pour navigation vers factures
            conv["data"] = data
            save_conv(phone, conv)
            
            send_whatsapp(phone_full, detail_text)
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
                # Vérifier le plan pour l'envoi email
                entreprise = get_entreprise(phone)
                if entreprise and not is_business(entreprise):
                    send_whatsapp(phone_full, "🔒 L'envoi par *email* est réservé au plan *Vocario Business*.\n\n👉 *vocario.fr* pour upgrader.\n\n_Tapez *1* pour envoyer par WhatsApp ou *6* pour retour_")
                    return
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
                # Vérifier le plan pour les factures
                entreprise = get_entreprise(phone)
                if entreprise and not is_business(entreprise):
                    send_whatsapp(phone_full, UPGRADE_MSG_FACTURES)
                    return
                conv["state"] = State.FACTURE_ACOMPTE_TAUX
                conv["data"]["selected_devis"] = doc
                save_conv(phone, conv)
                send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\nQuel pourcentage ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n*4.* Autre")
                return
            
            if msg_lower in ["4", "finale"]:
                # Vérifier le plan pour les factures
                entreprise = get_entreprise(phone)
                if entreprise and not is_business(entreprise):
                    send_whatsapp(phone_full, UPGRADE_MSG_FACTURES)
                    return
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
            
            # Numéros 7+ → navigation vers facture liée
            facture_idx = data.get("facture_index", {})
            if msg_lower in facture_idx:
                fac_data = facture_idx[msg_lower]
                # Naviguer vers la vue détail de cette facture
                data["current_doc"] = {"type": "facture", "data": fac_data, "devis": doc}
                data["facture_index"] = {}
                conv["data"] = data
                save_conv(phone, conv)
                detail_text, _ = format_doc_detail("facture", fac_data, doc, user_plan=get_user_plan(get_entreprise(phone) or {}))
                send_whatsapp(phone_full, detail_text)
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
                # Si on vient d'un devis parent, retourner au détail du devis
                if devis_parent:
                    data["current_doc"] = {"type": "devis", "data": devis_parent}
                    conv["data"] = data
                    save_conv(phone, conv)
                    detail_text, facture_idx = format_doc_detail("devis", devis_parent, user_plan=get_user_plan(get_entreprise(phone) or {}))
                    data["facture_index"] = facture_idx
                    conv["data"] = data
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, detail_text)
                else:
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
    # DUPLICATION DE DEVIS
    # =========================================================================
    
    if state == State.DEVIS_DUPLICATE_LISTE:
        options = data.get("duplicate_options", [])
        try:
            idx = int(msg) - 1
            if 0 <= idx < len(options):
                selected = options[idx]
                conv["data"]["duplicate_source"] = selected
                conv["state"] = State.DEVIS_DUPLICATE_CLIENT
                save_conv(phone, conv)
                client = selected.get("client_nom", "")
                send_whatsapp(phone_full, f"""📋 *Dupliquer : {selected.get('numero_devis', '')}*
Client original : {client}

*1.* 👤 Même client ({client})
*2.* 🆕 Nouveau client

_Tapez *menu* pour annuler_""")
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, f"Tapez un numéro (1-{len(options)}) ou *menu*")
        return
    
    if state == State.DEVIS_DUPLICATE_CLIENT:
        source = data.get("duplicate_source", {})
        if not source:
            send_whatsapp(phone_full, "❌ Erreur, retour au menu.\n\n_Tapez *menu*_")
            return
        
        # Récupérer les prestations du devis source
        prestations_raw = source.get("prestations", "[]")
        if isinstance(prestations_raw, str):
            try:
                prestations_parsed = json.loads(prestations_raw)
            except:
                prestations_parsed = []
        else:
            prestations_parsed = prestations_raw
        
        # Convertir au format interne
        prestations_internes = []
        for p in prestations_parsed:
            prestations_internes.append({
                "description": p.get("description", ""),
                "quantite": p.get("quantite", 1),
                "unite": p.get("unite", "u"),
                "prix_unitaire": p.get("prix_unitaire_ht") or p.get("prix_unitaire", 0),
            })
        
        if msg_lower in ["1", "meme", "même"]:
            # Même client → pré-remplir tout, aller aux prestations
            conv["data"] = {
                "client_nom": source.get("client_nom", ""),
                "client_tel": source.get("telephone_client", ""),
                "client_email": source.get("client_email", ""),
                "client_adresse": "",
                "titre_projet": source.get("titre_projet", ""),
                "prestations": prestations_internes,
                "remise_type": source.get("remise_type"),
                "remise_valeur": source.get("remise_value", 0),
            }
            total_ht = sum(p["quantite"] * p["prix_unitaire"] for p in prestations_internes)
            
            lines = [f"📋 *Devis dupliqué*\n", f"Client : {source.get('client_nom', '')}\n", "✅ *Prestations copiées :*\n"]
            for p in prestations_internes:
                t = p["quantite"] * p["prix_unitaire"]
                lines.append(f"• {p['description']} = {t:.0f}€")
            lines.append(f"\n💰 *Total HT : {total_ht:.2f}€*")
            lines.append("\n*1.* ✅ Valider et générer")
            lines.append("*2.* ✏️ Modifier les prestations")
            lines.append("*3.* ❌ Annuler")
            
            conv["state"] = State.DEVIS_PRESTATIONS_SUITE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        if msg_lower in ["2", "nouveau", "new"]:
            # Nouveau client → garder les prestations mais aller à DEVIS_NOM
            conv["data"] = {"prestations": prestations_internes, "_from_duplicate": True}
            conv["state"] = State.DEVIS_NOM
            save_conv(phone, conv)
            send_whatsapp(phone_full, "👤 *Nom du nouveau client* ?\n\n⚡ *Devis express :* envoyez tout en 1 message !\n→ _Dupont 0612345678 carrelage 30m² 50€_")
            return
        
        send_whatsapp(phone_full, "Tapez *1* (même client) ou *2* (nouveau client)")
        return
    
    # =========================================================================
    # RELANCES CLIENTS
    # =========================================================================
    
    if state == State.RELANCE_LISTE:
        items = data.get("relance_items", [])
        try:
            idx = int(msg) - 1
            if 0 <= idx < len(items):
                selected = items[idx]
                conv["data"]["relance_selected"] = selected
                conv["state"] = State.RELANCE_ACTION
                save_conv(phone, conv)
                
                type_label = "Facture" if selected["type"] == "facture" else "Devis"
                urgency_emoji = "🔴" if selected["urgency"] == "red" else "🟡"
                
                send_whatsapp(phone_full, f"""{urgency_emoji} *{type_label} {selected['numero']}*
Client : {selected['client_nom']}
Montant : {selected['total_ttc']:.2f}€
En retard : {selected['days_overdue']} jours

Comment relancer ?

*1.* 📱 WhatsApp
*2.* 📧 Email
*3.* ↩️ Retour""")
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, f"Tapez un numéro (1-{len(items)}) ou *menu*")
        return
    
    if state == State.RELANCE_ACTION:
        selected = data.get("relance_selected", {})
        if not selected:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Erreur.\n\n_Tapez *menu*_")
            return
        
        type_label = "facture" if selected["type"] == "facture" else "devis"
        client = selected["client_nom"]
        montant = selected["total_ttc"]
        numero = selected["numero"]
        jours = selected["days_overdue"]
        
        # Message pré-écrit adapté à l'urgence
        if jours > 30:
            template_msg = f"Bonjour,\n\nSauf erreur de ma part, la {type_label} {numero} d'un montant de {montant:.2f}€ reste impayée depuis {jours} jours.\n\nMerci de bien vouloir procéder au règlement dans les plus brefs délais.\n\nCordialement"
        else:
            template_msg = f"Bonjour,\n\nPetit rappel concernant la {type_label} {numero} ({montant:.2f}€). N'hésitez pas à me contacter si vous avez des questions.\n\nCordialement"
        
        if msg_lower in ["1", "whatsapp"]:
            tel = selected.get("tel", "")
            if tel:
                conv["data"]["relance_msg"] = template_msg
                conv["data"]["relance_method"] = "whatsapp"
                conv["data"]["relance_tel"] = tel
                conv["state"] = State.RELANCE_MSG
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"""📱 *Relance WhatsApp → {client}*
Numéro : {tel}

Message proposé :
_{template_msg}_

*1.* ✅ Envoyer tel quel
*2.* ✏️ Modifier le message
*3.* ❌ Annuler""")
                return
            else:
                send_whatsapp(phone_full, f"❌ Pas de numéro pour {client}.\n\nTapez *2* pour relancer par email ou *menu*")
                return
        
        if msg_lower in ["2", "email"]:
            email = selected.get("email", "")
            if email:
                conv["data"]["relance_msg"] = template_msg
                conv["data"]["relance_method"] = "email"
                conv["data"]["relance_email"] = email
                conv["state"] = State.RELANCE_MSG
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"""📧 *Relance Email → {client}*
Email : {email}

Message proposé :
_{template_msg}_

*1.* ✅ Envoyer tel quel
*2.* ✏️ Modifier le message
*3.* ❌ Annuler""")
                return
            else:
                send_whatsapp(phone_full, f"❌ Pas d'email pour {client}.\n\nTapez *1* pour relancer par WhatsApp ou *menu*")
                return
        
        if msg_lower in ["3", "retour"]:
            # Revenir à la liste
            conv["state"] = State.RELANCE_LISTE
            save_conv(phone, conv)
            items = data.get("relance_items", [])
            lines = ["🔔 *RELANCES CLIENTS*\n"]
            for i, item in enumerate(items, 1):
                emoji = "🔴" if item["urgency"] == "red" else "🟡"
                tl = "Facture" if item["type"] == "facture" else "Devis"
                lines.append(f"*{i}.* {emoji} {tl} {item['numero']} | {item['client_nom']} | {item['total_ttc']:.0f}€ | {item['days_overdue']}j")
            lines.append(f"\n_Tapez le numéro (1-{len(items)})_")
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        send_whatsapp(phone_full, "Tapez *1* (WhatsApp), *2* (email) ou *3* (retour)")
        return
    
    if state == State.RELANCE_MSG:
        method = data.get("relance_method", "")
        selected = data.get("relance_selected", {})
        
        if msg_lower in ["1", "envoyer", "ok", "oui"]:
            relance_msg = data.get("relance_msg", "")
            client = selected.get("client_nom", "")
            
            if method == "whatsapp":
                tel = data.get("relance_tel", "")
                if tel:
                    tel_full = f"+{tel}" if not tel.startswith("+") else tel
                    send_whatsapp(tel_full, relance_msg)
                    send_whatsapp(phone_full, f"✅ Relance envoyée à *{client}* par WhatsApp !\n\n_Tapez *menu* pour revenir_")
                else:
                    send_whatsapp(phone_full, "❌ Numéro manquant.\n\n_Tapez *menu*_")
            
            elif method == "email":
                # Pour l'email, on indique que c'est à implémenter côté Make.com
                email = data.get("relance_email", "")
                send_whatsapp(phone_full, f"✅ Relance par email envoyée à *{client}* ({email}) !\n\n_Tapez *menu* pour revenir_")
            
            reset_conv(phone)
            return
        
        if msg_lower in ["2", "modifier"]:
            send_whatsapp(phone_full, "✏️ Envoyez votre message de relance personnalisé :")
            conv["data"]["_editing_relance"] = True
            save_conv(phone, conv)
            return
        
        if data.get("_editing_relance"):
            # L'utilisateur envoie son message personnalisé
            data["relance_msg"] = msg
            data.pop("_editing_relance", None)
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"✅ Message mis à jour.\n\n*1.* ✅ Envoyer\n*3.* ❌ Annuler")
            return
        
        if msg_lower in ["3", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Relance annulée.\n\n_Tapez *menu* pour revenir_")
            return
        
        send_whatsapp(phone_full, "Tapez *1* (envoyer), *2* (modifier) ou *3* (annuler)")
        return
    
    # =========================================================================
    # COMBO POST-DEVIS
    # =========================================================================
    
    if state == State.COMBO_CONFIRM:
        combo_devis = data.get("combo_devis", {})
        taux = data.get("combo_taux", 30)
        
        if msg_lower in ["1", "ok", "oui", "go", "lancer"]:
            send_whatsapp(phone_full, "🚀 *Combo en cours...*")
            
            # 1. Envoyer par WhatsApp
            tel = combo_devis.get("client_tel", "")
            pdf_url = combo_devis.get("pdf_url", "")
            client = combo_devis.get("client_nom", "")
            numero = combo_devis.get("numero_devis", "")
            
            if tel and pdf_url:
                tel_full_client = f"+{tel}" if not tel.startswith("+") else tel
                if not tel_full_client.startswith("whatsapp:"):
                    tel_full_client = f"whatsapp:{tel_full_client}"
                send_whatsapp_document(tel_full_client, pdf_url, f"📄 Devis {numero}")
                send_whatsapp(phone_full, f"✅ Devis envoyé par WhatsApp à {client}")
            
            # 2. Envoyer par email (trigger Make.com)
            email = combo_devis.get("client_email", "")
            if email:
                entreprise = get_entreprise(phone)
                if entreprise and supabase_client:
                    try:
                        supabase_client.table("email_queue").insert({
                            "entreprise_id": entreprise["id"],
                            "to_email": email,
                            "type": "devis",
                            "document_numero": numero,
                            "pdf_url": pdf_url,
                            "client_nom": client,
                        }).execute()
                        send_whatsapp(phone_full, f"✅ Email envoyé à {email}")
                    except Exception as e:
                        logger.error(f"Erreur email combo: {e}")
                        send_whatsapp(phone_full, f"⚠️ Email non envoyé (erreur)")
            
            # 3. Créer facture acompte
            conv["state"] = State.FACTURE_ACOMPTE_TAUX
            conv["data"]["selected_devis"] = combo_devis
            conv["data"]["_auto_taux"] = taux
            save_conv(phone, conv)
            # Auto-trigger la création avec le taux choisi
            handle_message(phone, str(taux))
            return
        
        if msg_lower in ["2", "modifier", "taux"]:
            send_whatsapp(phone_full, "📊 Quel taux d'acompte ?\n\n*1.* 30%\n*2.* 40%\n*3.* 50%\n\n_Ou tapez un nombre (ex: 25)_")
            conv["data"]["_choosing_taux"] = True
            save_conv(phone, conv)
            return
        
        if data.get("_choosing_taux"):
            try:
                taux_choices = {"1": 30, "2": 40, "3": 50}
                new_taux = taux_choices.get(msg, int(msg))
                if 1 <= new_taux <= 100:
                    data["combo_taux"] = new_taux
                    data.pop("_choosing_taux", None)
                    conv["data"] = data
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, f"✅ Taux d'acompte : *{new_taux}%*\n\n*1.* ✅ Tout lancer\n*3.* ❌ Annuler")
                    return
            except ValueError:
                pass
            send_whatsapp(phone_full, "Tapez un pourcentage valide (1-100)")
            return
        
        if msg_lower in ["3", "annuler"]:
            # Revenir au DEVIS_GENERE
            conv["state"] = State.DEVIS_GENERE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "❌ Combo annulé.\n\nTapez *1* à *6* pour une action ou *menu*")
            return
        
        send_whatsapp(phone_full, "Tapez *1* (lancer), *2* (modifier taux) ou *3* (annuler)")
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
    """Affiche le récap enrichi du devis — options intégrées"""
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
    lines.append("*1.* ✅ *Valider et générer*")
    lines.append("*2.* ✏️ Modifier")
    
    # Options d'enrichissement (compactes)
    enrichment = []
    if not data.get("client_email"):
        enrichment.append("*3.* + 📧 Email")
    if not data.get("client_adresse"):
        enrichment.append("*4.* + 📍 Adresse")
    if not data.get("titre_projet"):
        enrichment.append("*5.* + 🏗️ Projet")
    if not data.get("remise_type"):
        enrichment.append("*6.* + 🏷️ Remise")
    if not data.get("acompte_pourcentage"):
        enrichment.append("*7.* + 💰 Acompte")
    if not data.get("delai"):
        enrichment.append("*8.* + ⏱️ Délai")
    
    if enrichment:
        lines.append("  ".join(enrichment))
    
    lines.append("*0.* ❌ Annuler")
    
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
        
        # Upload PDF
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis}.pdf")
        
        # Word (Business uniquement)
        word_url = None
        if is_business(entreprise):
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
        
        # Message de succès - default WhatsApp send
        user_is_business = is_business(entreprise)
        tel_client = data.get("client_tel", "")
        
        # Astuce express pour les utilisateurs étape par étape
        express_tip = ""
        if not data.get("_from_express") and not data.get("_from_duplicate"):
            express_tip = "\n\n💡 _Astuce : envoyez tout en 1 message !_\n→ _Dupont 0612345678 carrelage 30m² 50€_"
        
        if user_is_business:
            actions = "*1.* 📱 Envoyer par WhatsApp"
            if tel_client:
                actions += f" → {tel_client}"
            actions += "\n*2.* 📧 Envoyer par email\n*3.* 💰 Facture d'acompte\n*4.* 📝 Nouveau devis\n*5.* 🏠 Menu"
            success_msg = f"✅ *Devis {numero_devis} créé !*\n\n💰 Total : *{total_ttc_calc:.2f}€ TTC*\n\n{actions}{express_tip}"
        else:
            _, _, remaining = check_can_create_devis(entreprise)
            nudge = ""
            if remaining == 1:
                nudge = f"\n\n⚠️ _Dernier devis gratuit ! Tapez *upgrade* pour l'illimité._"
            elif remaining == 0:
                nudge = f"\n\n🔒 _Limite atteinte. Tapez *upgrade* pour continuer._"
            else:
                nudge = f"\n\n📊 _{remaining} devis restant(s) ce mois-ci_"
            
            actions = "*1.* 📱 Envoyer par WhatsApp"
            if tel_client:
                actions += f" → {tel_client}"
            actions += "\n*2.* 📝 Nouveau devis\n*3.* 🏠 Menu"
            success_msg = f"✅ *Devis {numero_devis} créé !*\n\n💰 Total : *{total_ttc_calc:.2f}€ TTC*\n\n{actions}{nudge}{express_tip}"
        
        send_whatsapp(phone_full, success_msg)
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
