"""
Vocario WhatsApp Handler v9 - Copain Pro
Module séparé avec APIRouter - s'intègre dans main.py via setup()

v9 Changes vs v8:
- Ton chaleureux ("C'est noté !", "Comment on l'envoie ?")
- Messages courts (≤ 6 lignes sauf récap)
- Navigation claire : ↩️ retour · 🏠 menu partout
- Mes Documents : uniquement des devis, factures en sous-ligne résumé
- Détail devis : factures liées en A/B/C, actions contextuelles
- Récap compact : "Compléter" regroupe les options optionnelles
- Erreurs douces (pas de ❌ agressif)
- Après chaque action → prochaine étape logique
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
    
    logger.info("✅ WhatsApp handler v9 setup complete")


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
# NAVIGATION FOOTER (v9)
# =============================================================================

NAV = "\n↩️ *retour* · 🏠 *menu*"
NAV_MENU_ONLY = "\n🏠 *menu*"


# =============================================================================
# ÉTATS DE CONVERSATION
# =============================================================================

class State:
    MENU = "menu"
    # Devis
    DEVIS_NOM = "devis_nom"
    DEVIS_CLIENT_SELECT = "devis_client_select"
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
    DEVIS_COMPLETER = "devis_completer"  # v9: regroupe les enrichissements
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
    # Post-envoi
    POST_ENVOI = "post_envoi"


# =============================================================================
# CACHE CONVERSATIONS (Supabase + RAM)
# =============================================================================

_conversations: Dict[str, Dict] = {}
_processed_sids: Dict[str, datetime] = {}


def normalize_phone(phone: str) -> str:
    return phone.replace("whatsapp:", "").replace("+", "").strip()


def get_conv(phone: str) -> Dict:
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
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning(f"Twilio non configuré, message non envoyé: {body[:50]}")
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        if not to.startswith("whatsapp:"):
            if not to.startswith("+"):
                to = f"+{to}"
            to = f"whatsapp:{to}"
        resp = requests.post(url, data={
            "From": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            "To": to,
            "Body": body,
        }, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
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
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
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
        }, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
        if resp.status_code in [200, 201]:
            return True
        else:
            logger.error(f"Erreur template Twilio {resp.status_code}: {resp.text[:200]}")
            send_whatsapp(to, "👋 *Bienvenue sur Vocario !*\n\nTapez:\n*1* → 📝 Nouveau devis\n*2* → 📂 Mes documents\n*3* → ❓ Aide")
            return True
    except Exception as e:
        logger.error(f"Erreur template: {e}")
        return False


def send_whatsapp_document(to: str, pdf_url: str, caption: str = ""):
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
        resp = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
        return resp.status_code in [200, 201]
    except Exception as e:
        logger.error(f"Erreur envoi document: {e}")
        return False


# =============================================================================
# FONCTIONS EMAIL (Resend) — identiques v8
# =============================================================================

def send_email_devis(to_email: str, entreprise: Dict, devis: Dict, avec_signature: bool = False):
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
    signature_html = ""
    if avec_signature:
        devis_uuid = devis.get("id", "")
        logger.info(f"🔗 SIGNATURE - devis_uuid: '{devis_uuid}' (type: {type(devis_uuid)})")
        logger.info(f"   devis keys: {list(devis.keys())}")
        if devis_uuid:
            signature_url = f"https://vocario.fr/signer/{devis_uuid}"
            logger.info(f"   URL signature: {signature_url}")
            signature_html = f'''
            <div style="text-align:center; margin:20px 0;">
                <a href="{signature_url}" style="background-color:{couleur}; color:white; padding:15px 30px; text-decoration:none; border-radius:8px; font-size:16px; font-weight:bold;">
                    ✍️ Signer le devis
                </a>
            </div>
            '''
        else:
            logger.error(f"❌ SIGNATURE - UUID vide ! devis data: {devis}")
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
        attachments = []
        if pdf_url and pdf_url.startswith("http"):
            try:
                pdf_resp = requests.get(pdf_url, timeout=15)
                if pdf_resp.status_code == 200:
                    import base64
                    attachments = [{"filename": f"{numero}.pdf", "content": base64.b64encode(pdf_resp.content).decode("utf-8")}]
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
                    attachments = [{"filename": f"{numero}.pdf", "content": base64.b64encode(pdf_resp.content).decode("utf-8")}]
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

import time as _time

# Cache entreprise (5 min TTL) pour réduire les queries Supabase
_entreprise_cache: Dict[str, tuple] = {}  # phone -> (data, timestamp)
_CACHE_TTL = 300  # 5 minutes

def get_entreprise(phone: str) -> Optional[Dict]:
    now = _time.time()
    if phone in _entreprise_cache:
        cached_data, ts = _entreprise_cache[phone]
        if now - ts < _CACHE_TTL:
            return cached_data
    data = get_entreprise_by_whatsapp(phone)
    if data:
        _entreprise_cache[phone] = (data, now)
    return data


def invalidate_entreprise_cache(phone: str):
    """Invalide le cache pour forcer un refresh (après upgrade plan, etc.)"""
    _entreprise_cache.pop(phone, None)


# ==================== GESTION DES PLANS ====================

FREE_DEVIS_LIMIT = 3

def get_user_plan(entreprise: Dict) -> str:
    """Retourne 'pro' ou 'free' basé sur le statut d'abonnement"""
    # Priorité 1 : subscription_status (géré par Stripe webhooks)
    sub_status = (entreprise.get("subscription_status") or "").lower().strip()
    if sub_status in ("active", "trialing"):
        return "pro"
    
    # Priorité 2 : champ plan legacy (migration)
    plan = (entreprise.get("plan") or entreprise.get("subscription") or "free").lower().strip()
    if plan in ["business", "pro", "premium", "paid"]:
        return "pro"
    
    return "free"


def count_devis_this_month(entreprise_id: str) -> int:
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
    plan = get_user_plan(entreprise)
    if plan == "pro":
        return True, "", -1
    count = count_devis_this_month(entreprise["id"])
    remaining = FREE_DEVIS_LIMIT - count
    if remaining <= 0:
        return False, f"📊 Vous avez atteint la limite de *{FREE_DEVIS_LIMIT} devis/mois* du plan gratuit.\n\n🚀 Passez à *Vocario Pro* pour tout débloquer !\n\n👉 *vocario.fr/upgrade*{NAV_MENU_ONLY}", 0
    return True, "", remaining


def is_pro(entreprise: Dict) -> bool:
    return get_user_plan(entreprise) == "pro"


UPGRADE_LINK = "vocario.fr/upgrade"

UPGRADE_MSG_FACTURES = f"🔒 Les *factures* sont réservées au plan *Vocario Pro* (15€ HT/mois).\n\n✅ Devis & factures illimités\n✅ Signature électronique\n✅ Relances automatiques\n\n👉 *{UPGRADE_LINK}*{NAV_MENU_ONLY}"

UPGRADE_MSG_RELANCES = f"🔒 Les *relances* sont réservées au plan *Vocario Pro*.\n\n👉 *{UPGRADE_LINK}*{NAV_MENU_ONLY}"


def get_devis_list(entreprise_id: str, limit: int = 10) -> List[Dict]:
    if not supabase_client:
        return []
    try:
        result = supabase_client.table("devis")\
            .select("id, numero_devis, client_nom, client_email, telephone_client, total_ht, total_ttc, statut, date, titre_projet, pdf_url, word_url, remise_type, remise_value, client_adresse")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        devis_list = result.data or []
        for d in devis_list:
            try:
                fac_result = supabase_client.table("factures")\
                    .select("id, numero_facture, total_ttc, statut, type_facture, date, pdf_url, client_nom, client_email, client_telephone")\
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
            .select("id, numero_facture, client_nom, total_ttc, statut, type_facture, date, pdf_url, devis_id, client_email, client_telephone")\
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
    if not supabase_client:
        return False
    try:
        supabase_client.table(table).update({"deleted_at": datetime.now().isoformat()}).eq("id", doc_id).execute()
        logger.info(f"Document supprimé: {table}/{doc_id}")
        return True
    except Exception as e:
        logger.error(f"Erreur suppression {table}/{doc_id}: {e}")
        return False


def update_document_status(table: str, doc_id: str, statut: str) -> bool:
    if not supabase_client:
        return False
    try:
        supabase_client.table(table).update({"statut": statut}).eq("id", doc_id).execute()
        return True
    except Exception as e:
        logger.error(f"Erreur update statut {table}/{doc_id}: {e}")
        return False


def get_devis_for_facture(entreprise_id: str) -> List[Dict]:
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
    stats = {"devis_en_attente": 0, "factures_impayees": 0, "montant_impaye": 0, "ca_mois": 0, "overdue_count": 0}
    if not supabase_client:
        return stats
    try:
        devis = supabase_client.table("devis")\
            .select("id, statut, total_ttc")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["en_attente", "envoye"])\
            .execute()
        stats["devis_en_attente"] = len(devis.data or [])
        factures = supabase_client.table("factures")\
            .select("id, statut, total_ttc, date, created_at")\
            .eq("entreprise_id", entreprise_id)\
            .is_("deleted_at", "null")\
            .in_("statut", ["en_attente", "envoyee"])\
            .execute()
        facs_impayees = factures.data or []
        stats["factures_impayees"] = len(facs_impayees)
        stats["montant_impaye"] = sum(f.get("total_ttc", 0) or 0 for f in facs_impayees)
        now = datetime.now()
        for f in facs_impayees:
            try:
                date_str = f.get("date") or f.get("created_at", "")
                if "T" in str(date_str):
                    fac_date = datetime.fromisoformat(str(date_str).replace("Z", ""))
                else:
                    fac_date = datetime.strptime(str(date_str), "%Y-%m-%d")
                if (now - fac_date).days > 30:
                    stats["overdue_count"] += 1
            except:
                pass
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
        presta_count = {}
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
        sorted_prestas = sorted(presta_count.values(), key=lambda x: x["count"], reverse=True)
        return sorted_prestas[:limit]
    except Exception as e:
        logger.error(f"Erreur get_frequent_prestations: {e}")
        return []


def get_overdue_documents(entreprise_id: str) -> List[Dict]:
    items = []
    if not supabase_client:
        return items
    try:
        now = datetime.now()
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
                        "type": "facture", "id": f.get("id"),
                        "numero": f.get("numero_facture", ""), "client_nom": f.get("client_nom", ""),
                        "total_ttc": f.get("total_ttc", 0), "days_overdue": days,
                        "tel": f.get("telephone_client", ""), "email": f.get("client_email", ""),
                        "urgency": "red" if days > 30 else "yellow"
                    })
            except:
                pass
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
                        "type": "devis", "id": d.get("id"),
                        "numero": d.get("numero_devis", ""), "client_nom": d.get("client_nom", ""),
                        "total_ttc": d.get("total_ttc", 0), "days_overdue": days,
                        "tel": d.get("telephone_client", ""), "email": d.get("client_email", ""),
                        "urgency": "yellow"
                    })
            except:
                pass
        items.sort(key=lambda x: (-1 if x["type"] == "facture" else 0, -x["days_overdue"]))
        return items[:10]
    except Exception as e:
        logger.error(f"Erreur get_overdue_documents: {e}")
        return []


def get_recent_devis_for_duplicate(entreprise_id: str, limit: int = 5) -> List[Dict]:
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


# =============================================================================
# PARSING PRESTATIONS - REGEX LOCAL (rapide, pas d'API)
# =============================================================================

def parse_prestations_regex(texte: str) -> List[Dict]:
    """Parse prestations avec regex — couvre 80% des cas simples, 0 latence"""
    prestations = []
    texte_clean = texte.replace("€", " €").replace("  ", " ").strip()
    lines = re.split(r'\n|(?:^|\s)\+\s', texte_clean)
    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue
        # Pattern 1: "Carrelage 30m2 50€"
        m = re.match(
            r'(.+?)\s+(\d+[.,]?\d*)\s*(m2|m²|ml|m|h|u|jours?|kg|l)\s*(?:[xX×àa@]\s*)?(\d+[.,]?\d*)\s*(?:€|euros?|eur)',
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
        # Pattern 2: "Peinture forfait 800€"
        m = re.match(r'(.+?)\s+(?:forfait\s+)?(\d+[.,]?\d*)\s*(?:€|euros?|eur)', line, re.IGNORECASE)
        if m:
            desc = m.group(1).strip().rstrip('-–—:').strip()
            prix = float(m.group(2).replace(',', '.'))
            if desc and not desc.replace(' ', '').isdigit() and prix > 0:
                prestations.append({"description": desc.capitalize(), "quantite": 1, "unite": "forfait", "prix_unitaire": prix})
                continue
        # Pattern 3: "800€ peinture"
        m = re.match(r'(\d+[.,]?\d*)\s*(?:€|euros?|eur)\s+(.+)', line, re.IGNORECASE)
        if m:
            prix = float(m.group(1).replace(',', '.'))
            desc = m.group(2).strip()
            if desc and prix > 0:
                prestations.append({"description": desc.capitalize(), "quantite": 1, "unite": "forfait", "prix_unitaire": prix})
                continue
    if not prestations:
        for pattern_fn in [
            lambda t: re.match(r'(.+?)\s+(\d+[.,]?\d*)\s*(m2|m²|ml|m|h|u|jours?|kg|l)\s*(?:[xX×àa@]\s*)?(\d+[.,]?\d*)\s*(?:€|euros?|eur)', t, re.IGNORECASE),
            lambda t: re.match(r'(.+?)\s+(?:forfait\s+)?(\d+[.,]?\d*)\s*(?:€|euros?|eur)', t, re.IGNORECASE),
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
    phone_match = re.search(r'(0\d[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})', texte)
    price_match = re.search(r'\d+[.,]?\d*\s*(?:€|euros?|eur)', texte, re.IGNORECASE)
    if not phone_match or not price_match:
        return None
    tel = re.sub(r'[^0-9]', '', phone_match.group(1))
    if len(tel) < 10:
        return None
    before_phone = texte[:phone_match.start()].strip()
    after_phone = texte[phone_match.end():].strip()
    if not before_phone or not after_phone:
        return None
    prestations = parse_prestations_regex(after_phone)
    if not prestations:
        return None
    return {"client_nom": before_phone.strip().title(), "client_tel": tel, "prestations": prestations}


# =============================================================================
# IA - PARSING PRESTATIONS (Claude Haiku - fallback)
# =============================================================================

def parse_prestations_ia(texte: str) -> List[Dict]:
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
JAMAIS de texte autour du JSON.""",
            messages=[{"role": "user", "content": texte}],
        )
        raw = response.content[0].text.strip()
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
    if not openai_whisper_client:
        return ""
    try:
        twilio_sid = TWILIO_ACCOUNT_SID
        twilio_token = TWILIO_AUTH_TOKEN
        if twilio_sid and twilio_token:
            resp = requests.get(audio_url, auth=(twilio_sid, twilio_token), timeout=15)
        else:
            resp = requests.get(audio_url, timeout=15)
        if resp.status_code != 200:
            return ""
        temp_file = f"/tmp/audio_{uuid.uuid4().hex}.ogg"
        try:
            with open(temp_file, "wb") as f:
                f.write(resp.content)
            with open(temp_file, "rb") as audio_file:
                transcript = openai_whisper_client.audio.transcriptions.create(
                    model="whisper-1", file=audio_file, language="fr"
                )
            return transcript.text.strip()
        finally:
            try:
                os.remove(temp_file)
            except:
                pass
    except Exception as e:
        logger.error(f"Erreur Whisper: {e}")
        return ""


# =============================================================================
# FORMATAGE — v9 : plus propre, plus clair
# =============================================================================

def auto_titre_projet(prestations: List[Dict]) -> str:
    """Génère un titre de projet court à partir des descriptions des prestations"""
    if not prestations:
        return "Travaux"
    
    # Extraire les descriptions nettoyées
    descriptions = []
    for p in prestations:
        desc = p.get("description", "").strip()
        if not desc:
            continue
        # Garder juste le premier mot significatif (ex: "Carrelage sol cuisine" → "Carrelage")
        # Sauf si c'est court, on garde tel quel
        if len(desc) > 25:
            desc = desc.split()[0].capitalize() if desc.split() else desc
        descriptions.append(desc)
    
    if not descriptions:
        return "Travaux"
    
    if len(descriptions) == 1:
        return descriptions[0][:50]
    
    if len(descriptions) == 2:
        titre = f"{descriptions[0]} & {descriptions[1]}"
    else:
        titre = f"{descriptions[0]}, {descriptions[1]} & {descriptions[2]}"
    
    # Tronquer si trop long
    if len(titre) > 50:
        titre = titre[:47] + "..."
    
    return titre


def fmt_amount(amount) -> str:
    """Formate un montant : 2760 → '2 760€', 800.50 → '800,50€'"""
    try:
        amount = float(amount)
    except:
        return "0€"
    if amount == int(amount):
        # Entier : espace milliers
        return f"{int(amount):,}€".replace(",", " ")
    else:
        return f"{amount:,.2f}€".replace(",", " ").replace(".", ",")


def fmt_statut_devis(statut: str, factures: List[Dict] = None) -> str:
    """Statut devis en français clair (v9)"""
    factures = factures or []
    nb_fac = len(factures)
    nb_acomptes = sum(1 for f in factures if f.get("type_facture") == "acompte")
    nb_payees = sum(1 for f in factures if f.get("statut") in ("payee", "paye"))
    has_finale = any(f.get("type_facture") != "acompte" for f in factures)
    
    base = {
        "en_attente": "🆕 Pas encore envoyé",
        "envoye": "📤 Envoyé, en attente",
        "signe": "✅ Signé",
        "accepte": "✅ Accepté",
        "refuse": "❌ Refusé",
        "annule": "🚫 Annulé",
    }.get(statut, f"⏳ {statut}")
    
    # Si toutes les factures sont payées et il y a une finale → tout réglé
    if has_finale and nb_fac > 0 and nb_payees == nb_fac:
        return "💰 Tout réglé"
    
    return base


def fmt_factures_summary(factures: List[Dict]) -> str:
    """Résumé compact des factures liées pour la liste documents (v9)"""
    if not factures:
        return ""
    
    parts = []
    nb_acomptes = sum(1 for f in factures if f.get("type_facture") == "acompte")
    nb_finales = sum(1 for f in factures if f.get("type_facture") != "acompte")
    nb_payees = sum(1 for f in factures if f.get("statut") in ("payee", "paye"))
    nb_a_encaisser = len(factures) - nb_payees
    
    if nb_acomptes > 0:
        parts.append(f"{nb_acomptes} acompte{'s' if nb_acomptes > 1 else ''}")
    if nb_finales > 0:
        parts.append(f"{nb_finales} facture{'s' if nb_finales > 1 else ''} finale{'s' if nb_finales > 1 else ''}")
    
    summary = " + ".join(parts)
    
    if nb_payees > 0 and nb_a_encaisser > 0:
        summary += f" ({nb_payees} payée{'s' if nb_payees > 1 else ''}, {nb_a_encaisser} à encaisser)"
    elif nb_payees > 0:
        summary += f" ({nb_payees} payée{'s' if nb_payees > 1 else ''})"
    elif nb_a_encaisser > 0:
        summary += f" ({nb_a_encaisser} à encaisser)"
    
    return summary


def format_documents_list(devis_list: List[Dict], factures_orphelines: List[Dict]) -> tuple:
    """v9 : Liste propre — uniquement des devis, factures en résumé sous chaque devis"""
    if not devis_list and not factures_orphelines:
        return "📂 *Aucun document pour le moment*\n\nCréez votre premier devis en tapant *1* !" + NAV_MENU_ONLY, {}
    
    doc_index = {}
    idx = 1
    
    # Compteurs pour le résumé en haut
    nb_devis_en_cours = sum(1 for d in devis_list if d.get("statut") in ("en_attente", "envoye", "signe", "accepte"))
    nb_fac_a_encaisser = 0
    for d in devis_list:
        for f in d.get("factures", []):
            if f.get("statut") not in ("payee", "paye"):
                nb_fac_a_encaisser += 1
    for f in factures_orphelines:
        if f.get("statut") not in ("payee", "paye"):
            nb_fac_a_encaisser += 1
    
    # Header
    lines = ["📂 *Mes documents*\n"]
    summary_parts = []
    if nb_devis_en_cours > 0:
        summary_parts.append(f"{nb_devis_en_cours} devis en cours")
    if nb_fac_a_encaisser > 0:
        summary_parts.append(f"{nb_fac_a_encaisser} facture{'s' if nb_fac_a_encaisser > 1 else ''} à encaisser")
    if summary_parts:
        lines.append(f"💡 {' · '.join(summary_parts)}")
    
    lines.append("\n━━━━━━━━━━━━")
    
    # Tri par urgence
    def sort_key(d):
        statut = d.get("statut", "en_attente")
        order = {"en_attente": 0, "envoye": 1, "signe": 2, "accepte": 3, "refuse": 4, "annule": 5}
        has_unpaid = any(f.get("statut") not in ("payee", "paye") for f in d.get("factures", []))
        if has_unpaid:
            return -1  # Factures impayées en premier
        return order.get(statut, 3)
    
    sorted_devis = sorted(devis_list, key=sort_key)
    
    for d in sorted_devis:
        client = d.get("client_nom", "Sans nom")
        projet = d.get("titre_projet", "")
        total = d.get("total_ttc", 0)
        factures = d.get("factures", [])
        statut_txt = fmt_statut_devis(d.get("statut", "en_attente"), factures)
        
        # Ligne principale : Client — Projet
        label = f"{client} — {projet}" if projet else client
        lines.append(f"\n*{idx}.* {label}")
        lines.append(f"     {fmt_amount(total)} · {statut_txt}")
        
        # Résumé factures en sous-ligne
        fac_summary = fmt_factures_summary(factures)
        if fac_summary:
            lines.append(f"     📎 {fac_summary}")
        
        doc_index[str(idx)] = {"type": "devis", "data": d}
        idx += 1
    
    # Factures orphelines (rare, mais géré)
    if factures_orphelines:
        lines.append("\n━━━━━━━━━━━━")
        lines.append("🧾 *Autres factures*\n")
        for f in factures_orphelines:
            client = f.get("client_nom", "")
            fac_type = "(acompte)" if f.get("type_facture") == "acompte" else ""
            total = f.get("total_ttc", 0)
            statut = "💰 Payée" if f.get("statut") in ("payee", "paye") else "💸 À encaisser"
            lines.append(f"*{idx}.* {client} {fac_type}")
            lines.append(f"     {fmt_amount(total)} · {statut}")
            doc_index[str(idx)] = {"type": "facture", "data": f}
            idx += 1
    
    lines.append("\n━━━━━━━━━━━━")
    lines.append(f"Tapez un numéro (1-{idx - 1}) pour ouvrir")
    lines.append(NAV_MENU_ONLY.strip())
    
    return "\n".join(lines), doc_index


def format_doc_detail(doc_type: str, doc: Dict, devis_parent: Dict = None, user_plan: str = "pro") -> tuple:
    """v9 : Détail document — contextuel, factures en A/B/C, actions adaptées"""
    lines = []
    facture_index = {}
    is_free = (user_plan != "pro")
    
    if doc_type == "devis":
        client = doc.get("client_nom", "")
        projet = doc.get("titre_projet", "")
        tel = doc.get("telephone_client", "")
        email = doc.get("client_email", "")
        total = doc.get("total_ttc", 0)
        statut_raw = doc.get("statut", "en_attente")
        factures = doc.get("factures", [])
        
        # Header compact
        lines.append(f"📋 *Devis — {client}*")
        if projet:
            lines.append(f"{projet} · *{fmt_amount(total)} TTC*")
        else:
            lines.append(f"*{fmt_amount(total)} TTC*")
        
        # Contact sur une ligne
        contact_parts = []
        if tel:
            contact_parts.append(f"📞 {tel}")
        if email:
            contact_parts.append(f"📧 {email}")
        if contact_parts:
            lines.append(" · ".join(contact_parts))
        
        lines.append(fmt_statut_devis(statut_raw, factures))
        
        # Factures liées avec lettres A/B/C
        if factures:
            lines.append("")
            lines.append("📎 *Factures :*")
            
            total_acomptes_payes = 0
            letters = "ABCDEFGHIJ"
            for i, f in enumerate(factures):
                letter = letters[i] if i < len(letters) else str(i + 1)
                ft_label = "Acompte" if f.get("type_facture") == "acompte" else "Facture finale"
                f_total = f.get("total_ttc", 0)
                f_statut = "💰 Payée" if f.get("statut") in ("payee", "paye") else "💸 À encaisser"
                lines.append(f"  *{letter}.* {ft_label} {fmt_amount(f_total)} · {f_statut}")
                facture_index[letter.lower()] = f
                
                if f.get("type_facture") == "acompte" and f.get("statut") in ("payee", "paye"):
                    total_acomptes_payes += float(f_total)
            
            # Reste à facturer
            has_finale = any(f.get("type_facture") != "acompte" for f in factures)
            if total_acomptes_payes > 0 and not has_finale:
                reste = float(total) - total_acomptes_payes
                if reste > 0:
                    lines.append(f"\n📊 *Reste à facturer : {fmt_amount(reste)}*")
        
        lines.append("\n━━━━━━━━━━━━")
        
        # Actions contextuelles v9
        action_num = 1
        
        # Déterminer les actions pertinentes selon le contexte
        has_finale = any(f.get("type_facture") != "acompte" for f in factures)
        all_paid = factures and all(f.get("statut") in ("payee", "paye") for f in factures) and has_finale
        
        if not all_paid:
            # Actions d'envoi
            lines.append(f"*{action_num}.* 📱 Envoyer WhatsApp")
            action_num += 1
            
            if is_free:
                lines.append(f"*{action_num}.* 📧 Envoyer email 🔒")
            else:
                lines.append(f"*{action_num}.* 📧 Envoyer email + signature ✍️")
            action_num += 1
            
            # Actions de facturation (Pro)
            if not is_free and not has_finale:
                total_acomptes = sum(float(f.get("total_ttc", 0)) for f in factures if f.get("type_facture") == "acompte" and f.get("statut") in ("payee", "paye"))
                reste = float(total) - total_acomptes
                
                if statut_raw in ("signe", "accepte"):
                    if total_acomptes > 0:
                        lines.append(f"*{action_num}.* 🧾 Facturer le solde ({fmt_amount(reste)})")
                    else:
                        lines.append(f"*{action_num}.* 💰 Facture d'acompte")
                        action_num += 1
                        lines.append(f"*{action_num}.* 🧾 Facture finale")
                else:
                    lines.append(f"*{action_num}.* 💰 Facture d'acompte")
                    action_num += 1
                    lines.append(f"*{action_num}.* 🧾 Facture finale")
                action_num += 1
            elif is_free:
                lines.append(f"*{action_num}.* 💰 Facturer 🔒")
                action_num += 1
        
        # Modifier (si pas encore envoyé)
        if statut_raw == "en_attente":
            lines.append(f"*{action_num}.* ✏️ Modifier")
            action_num += 1
        
        lines.append(f"*{action_num}.* 🗑️ Supprimer")
        suppr_num = action_num
        action_num += 1
        
        if facture_index:
            lines.append(f"\nTapez *A*, *B*... pour ouvrir une facture")
        
        lines.append(f"\n↩️ *retour* · 🏠 *menu*")
        
        # Store action mapping in conv data
        # We return it as part of the tuple for the handler to use
        action_map = _build_devis_action_map(doc, factures, is_free, statut_raw)
        
        return "\n".join(lines), facture_index, action_map
    
    elif doc_type == "facture":
        numero = doc.get("numero_facture", "")
        client = doc.get("client_nom", "")
        total = doc.get("total_ttc", 0)
        statut_raw = doc.get("statut", "en_attente")
        fac_type = "Acompte" if doc.get("type_facture") == "acompte" else "Facture"
        is_paid = statut_raw in ("payee", "paye")
        
        f_statut = "💰 Payée" if is_paid else "💸 À encaisser"
        
        lines.append(f"🧾 *{fac_type} — {client}*")
        lines.append(f"{numero} · *{fmt_amount(total)} TTC*")
        lines.append(f_statut)
        
        if devis_parent:
            dp_projet = devis_parent.get("titre_projet", "")
            dp_num = devis_parent.get("numero_devis", "")
            if dp_projet:
                lines.append(f"📎 Devis : {dp_projet} ({dp_num})")
            else:
                lines.append(f"📎 Devis : {dp_num}")
        
        lines.append("\n━━━━━━━━━━━━")
        
        if is_paid:
            lines.append("*1.* 📱 Renvoyer WhatsApp")
            lines.append("*2.* 📧 Renvoyer email")
            lines.append("*3.* 🗑️ Supprimer")
        else:
            lines.append("*1.* 📱 Envoyer WhatsApp")
            lines.append("*2.* 📧 Envoyer email")
            lines.append("*3.* ✅ Marquer payée")
            lines.append("*4.* 🗑️ Supprimer")
        
        lines.append(f"\n↩️ *retour* · 🏠 *menu*")
        
        return "\n".join(lines), facture_index, {}
    
    return "", {}, {}


def _build_devis_action_map(doc: Dict, factures: List[Dict], is_free: bool, statut_raw: str) -> Dict:
    """Construit le mapping action_num → action_name pour le détail devis (v9)"""
    action_map = {}
    num = 1
    has_finale = any(f.get("type_facture") != "acompte" for f in factures)
    all_paid = factures and all(f.get("statut") in ("payee", "paye") for f in factures) and has_finale
    
    if not all_paid:
        action_map[str(num)] = "whatsapp"
        num += 1
        action_map[str(num)] = "email"
        num += 1
        
        if not is_free and not has_finale:
            total_acomptes = sum(float(f.get("total_ttc", 0)) for f in factures if f.get("type_facture") == "acompte" and f.get("statut") in ("payee", "paye"))
            
            if statut_raw in ("signe", "accepte") and total_acomptes > 0:
                action_map[str(num)] = "facture_finale"
                num += 1
            else:
                action_map[str(num)] = "facture_acompte"
                num += 1
                action_map[str(num)] = "facture_finale"
                num += 1
        elif is_free:
            action_map[str(num)] = "facturer_locked"
            num += 1
    
    if statut_raw == "en_attente":
        action_map[str(num)] = "modifier"
        num += 1
    
    action_map[str(num)] = "supprimer"
    
    return action_map


# =============================================================================
# HANDLER PRINCIPAL - STATE MACHINE (v9)
# =============================================================================

_cleanup_counter = 0

def _cleanup_stale_data():
    """Purge conversations inactives > 2h et caches expirés"""
    now = datetime.now()
    now_ts = _time.time()
    
    # Conversations RAM
    stale = [p for p, c in _conversations.items()
             if (now - c.get("last_activity", now)).total_seconds() > 7200]
    for p in stale:
        del _conversations[p]
    
    # Cache entreprise expiré
    stale_cache = [p for p, (_, ts) in _entreprise_cache.items()
                   if now_ts - ts > _CACHE_TTL * 2]
    for p in stale_cache:
        del _entreprise_cache[p]
    
    # Dedup SIDs vieux
    old_sids = [s for s, t in _processed_sids.items()
                if (now - t).total_seconds() > 300]
    for s in old_sids:
        del _processed_sids[s]
    
    if stale or stale_cache:
        logger.info(f"🧹 Cleanup: {len(stale)} convs, {len(stale_cache)} cache, {len(old_sids)} sids")


def handle_message(phone: str, message: str, media_url: str = None, media_type: str = None, button_payload: str = None):
    """Gère un message WhatsApp entrant — v9 copain pro"""
    global _cleanup_counter
    _cleanup_counter += 1
    if _cleanup_counter % 50 == 0:
        _cleanup_stale_data()
    
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
            send_whatsapp(phone_full, "Hmm, je n'ai pas compris le vocal 🤔\nEssayez de parler plus fort, ou écrivez votre message." + NAV_MENU_ONLY)
            return
    
    if not msg and not button_payload:
        send_whatsapp(phone_full, "👋 Tapez *menu* pour commencer !")
        return
    
    conv = get_conv(phone)
    state = conv.get("state", State.MENU)
    data = conv.get("data", {})
    
    logger.info(f"[{phone}] state={state} msg='{msg_lower[:50]}' button={button_payload}")
    
    # =========================================================================
    # COMMANDES GLOBALES
    # =========================================================================
    
    if msg_lower in ["menu", "start", "bonjour", "salut", "hello", "accueil", "0"]:
        reset_conv(phone)
        entreprise = get_entreprise(phone)
        if entreprise:
            user_is_pro = is_pro(entreprise)
            # Récupérer le prénom/nom du gérant
            gerant = entreprise.get("gerant", "")
            prenom = gerant.split()[0] if gerant else ""
            greeting = f"👋 Bonjour{' ' + prenom if prenom else ''} !"
            
            if user_is_pro:
                stats = get_activity_dashboard(entreprise["id"])
                dashboard_parts = []
                if stats["devis_en_attente"] > 0:
                    dashboard_parts.append(f"📝 {stats['devis_en_attente']} devis en attente")
                if stats["factures_impayees"] > 0:
                    dashboard_parts.append(f"🔴 {stats['factures_impayees']} facture(s) impayée(s) — {fmt_amount(stats['montant_impaye'])}")
                if stats["overdue_count"] > 0:
                    dashboard_parts.append(f"⚠️ {stats['overdue_count']} en retard > 30j")
                if stats["ca_mois"] > 0:
                    dashboard_parts.append(f"💰 CA du mois : {fmt_amount(stats['ca_mois'])}")
                
                if dashboard_parts:
                    send_whatsapp(phone_full, f"{greeting}\n\n📊 *Votre activité*\n" + "\n".join(dashboard_parts) + "\n\nQue fait-on ?")
                else:
                    send_whatsapp(phone_full, f"{greeting}\n\nQue fait-on ?")
            else:
                _, _, remaining = check_can_create_devis(entreprise)
                used = FREE_DEVIS_LIMIT - remaining
                bar = "█" * used + "░" * remaining
                counter = f"📊 Devis ce mois : *{used}/{FREE_DEVIS_LIMIT}* {bar}"
                if remaining <= 1 and remaining > 0:
                    counter += f"\n⚠️ Plus qu'{remaining} devis gratuit !"
                elif remaining == 0:
                    counter += "\n🔒 Limite atteinte — tapez *upgrade*"
                send_whatsapp(phone_full, f"{greeting}\n\n{counter}\n\nQue fait-on ?")
        else:
            send_whatsapp(phone_full, "👋 Bienvenue sur Vocario !\n\nQue fait-on ?")
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    if msg_lower in ["annuler", "cancel", "stop"]:
        reset_conv(phone)
        send_whatsapp(phone_full, "❌ Annulé." + NAV_MENU_ONLY)
        return
    
    if msg_lower in ["upgrade", "business", "passer pro", "abonnement"]:
        send_whatsapp(phone_full, f"""🚀 *Vocario Pro* — 15€ HT/mois

✅ Devis & factures *illimités*
✅ Signature électronique
✅ Factures d'acompte en 1 clic
✅ Relances clients
✅ Export Word + PDF

💡 _Un seul devis signé rembourse 1 an !_

👉 *{UPGRADE_LINK}*{NAV_MENU_ONLY}""")
        return
    
    # Raccourcis globaux depuis n'importe quel état
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
            handle_message(phone, message, button_payload=button_payload)
            return
    
    if msg_lower == "retour":
        retour_map = {
            State.DEVIS_TEL: State.DEVIS_NOM,
            State.DEVIS_PRESTATIONS: State.DEVIS_TEL,
            State.DEVIS_RECAP: State.DEVIS_PRESTATIONS,
            State.DEVIS_EMAIL: State.DEVIS_RECAP,
            State.DEVIS_ADRESSE: State.DEVIS_RECAP,
            State.DEVIS_PROJET: State.DEVIS_RECAP,
            State.DEVIS_REMISE: State.DEVIS_RECAP,
            State.DEVIS_ACOMPTE: State.DEVIS_RECAP,
            State.DEVIS_DELAI: State.DEVIS_RECAP,
            State.DEVIS_COMPLETER: State.DEVIS_RECAP,
            State.DOCS_DETAIL: State.DOCS_LISTE,
            State.POST_ENVOI: State.MENU,
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
        # Nouveau devis
        if button_payload in ["nouveau_devis", "new_devis", "Nouveau devis"] or msg_lower in ["1", "devis", "nouveau devis", "créer devis", "nouveau", "new"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "Configurez d'abord votre profil sur *vocario.fr* 🏗️" + NAV_MENU_ONLY)
                return
            ok, limit_msg, remaining = check_can_create_devis(entreprise)
            if not ok:
                send_whatsapp(phone_full, limit_msg)
                return
            
            # Auto-complétion clients (Pro)
            if is_pro(entreprise):
                clients = get_recent_clients(entreprise["id"])
                if clients:
                    lines = ["📝 *Nouveau devis*\n", "👤 Choisissez un client récent :\n"]
                    for i, c in enumerate(clients, 1):
                        lines.append(f"*{i}.* {c['nom']}")
                    lines.append(f"*{len(clients) + 1}.* 🆕 Nouveau client")
                    lines.append(NAV_MENU_ONLY.strip())
                    conv["state"] = State.DEVIS_CLIENT_SELECT
                    conv["data"] = {"recent_clients": clients}
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, "\n".join(lines))
                    return
            
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"""📝 *Nouveau devis*

👤 Nom du client ?

💡 _Astuce : envoyez tout d'un coup !_
_Ex: Dupont 0612345678 carrelage 30m² 50€_{NAV_MENU_ONLY}""")
            return
        
        # Mes documents
        if button_payload in ["mes_documents", "documents", "Mes documents"] or msg_lower in ["2", "documents", "mes documents", "docs", "mes docs"]:
            _show_documents(phone, phone_full, conv)
            return
        
        # Facture → rediriger
        if msg_lower in ["facture", "nouvelle facture", "créer facture"]:
            send_whatsapp(phone_full, "🧾 Pour créer une facture, ouvrez un devis depuis *Mes documents* et choisissez *Facturer*.")
            _show_documents(phone, phone_full, conv)
            return
        
        # Aide
        if button_payload in ["aide", "help", "Aide"] or msg_lower in ["3", "aide", "help"]:
            send_whatsapp(phone_full, f"""❓ *Aide rapide*

📝 *1* → Nouveau devis
📂 *2* → Mes documents
⚡ Devis express → _Dupont 06... carrelage 30m² 50€_
🎤 Envoyez un vocal, ça marche !

💬 Besoin d'aide ? *contact@vocario.fr*{NAV_MENU_ONLY}""")
            return
        
        # Dupliquer (Pro)
        if msg_lower in ["4", "dupliquer", "copier", "dupliquer devis"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
                return
            if not is_pro(entreprise):
                send_whatsapp(phone_full, f"🔒 La *duplication* est réservée au plan Pro.\n\n👉 *{UPGRADE_LINK}*{NAV_MENU_ONLY}")
                return
            devis_list = get_recent_devis_for_duplicate(entreprise["id"])
            if not devis_list:
                send_whatsapp(phone_full, "📭 Aucun devis à dupliquer." + NAV_MENU_ONLY)
                return
            lines = ["📋 *Dupliquer un devis*\n"]
            for i, d in enumerate(devis_list, 1):
                client = d.get("client_nom", "")
                total = d.get("total_ttc", 0)
                projet = d.get("titre_projet", "")
                label = f"*{i}.* {client} — {fmt_amount(total)}"
                if projet:
                    label += f" — {projet[:20]}"
                lines.append(label)
            lines.append(NAV.strip())
            conv["state"] = State.DEVIS_DUPLICATE_LISTE
            conv["data"] = {"duplicate_options": devis_list}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        # Relances (Pro)
        if msg_lower in ["5", "relance", "relances", "relancer"]:
            entreprise = get_entreprise(phone)
            if not entreprise:
                send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
                return
            if not is_pro(entreprise):
                send_whatsapp(phone_full, UPGRADE_MSG_RELANCES)
                return
            overdue = get_overdue_documents(entreprise["id"])
            if not overdue:
                send_whatsapp(phone_full, "✅ *Rien à relancer !* Tout est à jour 👏" + NAV_MENU_ONLY)
                return
            lines = ["🔔 *Relances*\n"]
            for i, item in enumerate(overdue, 1):
                emoji = "🔴" if item["urgency"] == "red" else "🟡"
                type_label = "Facture" if item["type"] == "facture" else "Devis"
                lines.append(f"*{i}.* {emoji} {type_label} · {item['client_nom']} · {fmt_amount(item['total_ttc'])} · {item['days_overdue']}j")
            lines.append(NAV.strip())
            conv["state"] = State.RELANCE_LISTE
            conv["data"] = {"relance_items": overdue}
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        # Texte non reconnu → menu
        send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
        return
    
    # =========================================================================
    # FLOW DEVIS
    # =========================================================================
    
    if state == State.DEVIS_CLIENT_SELECT:
        clients = data.get("recent_clients", [])
        new_client_num = str(len(clients) + 1)
        if msg_lower in [new_client_num, "nouveau", "new", "autre"]:
            conv["state"] = State.DEVIS_NOM
            conv["data"] = {}
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"👤 Nom du client ?\n\n💡 _Ou tout d'un coup : Dupont 06... carrelage 30m² 50€_{NAV}")
            return
        try:
            idx = int(msg) - 1
            if 0 <= idx < len(clients):
                selected = clients[idx]
                conv["data"] = {
                    "client_nom": selected["nom"],
                    "client_tel": selected.get("tel", ""),
                    "client_email": selected.get("email", ""),
                    "client_adresse": selected.get("adresse", ""),
                }
                conv["state"] = State.DEVIS_PRESTATIONS
                save_conv(phone, conv)
                
                # Favoris prestations
                favorites_msg = _get_favorites_msg(phone, conv)
                
                send_whatsapp(phone_full, f"✅ *{selected['nom']}*\n\n🔨 Décrivez les travaux et les prix :\n_Ex: Carrelage 30m² 50€, Peinture salon 800€_\n🎤 Le vocal marche aussi !{favorites_msg}{NAV}")
                return
        except ValueError:
            pass
        # Texte libre = nouveau nom
        conv["data"] = {"client_nom": msg}
        conv["state"] = State.DEVIS_TEL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ *{msg}*\n\n📞 Son numéro ?\n_Ex: 06 12 34 56 78_{NAV}")
        return
    
    if state == State.DEVIS_NOM:
        if msg == "__show__":
            send_whatsapp(phone_full, f"👤 Nom du client ?\n\n💡 _Ou tout d'un coup : Dupont 06... carrelage 30m² 50€_{NAV}")
            return
        # Mode express
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
                    presta_lines.append(f"• {p['description']} = {fmt_amount(t)}")
                else:
                    presta_lines.append(f"• {p['description']} {p['quantite']} {p['unite']} × {p['prix_unitaire']:.0f}€ = {fmt_amount(t)}")
            send_whatsapp(phone_full, f"⚡ *Devis express !*\n\n👤 {express['client_nom']} · 📞 {express['client_tel']}\n{chr(10).join(presta_lines)}\n💰 *Total HT : {fmt_amount(total_ht)}*")
            _show_recap(phone, phone_full, conv)
            return
        
        data["client_nom"] = msg
        conv["data"] = data
        conv["state"] = State.DEVIS_TEL
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ *{msg}*\n\n📞 Son numéro ?\n_Ex: 06 12 34 56 78_{NAV}")
        return
    
    if state == State.DEVIS_TEL:
        if msg == "__show__":
            send_whatsapp(phone_full, f"📞 Numéro du client ?\n_Ex: 06 12 34 56 78_{NAV}")
            return
        tel = re.sub(r'[^0-9+]', '', msg)
        if len(tel) < 10:
            send_whatsapp(phone_full, "Hmm, ce numéro semble incorrect 🤔\nIl faut 10 chiffres, ex: *06 12 34 56 78*")
            return
        data["client_tel"] = tel
        conv["data"] = data
        conv["state"] = State.DEVIS_PRESTATIONS
        save_conv(phone, conv)
        favorites_msg = _get_favorites_msg(phone, conv)
        send_whatsapp(phone_full, f"✅ *{tel}*\n\n🔨 Décrivez les travaux et les prix :\n_Ex: Carrelage 30m² 50€, Peinture salon 800€_\n🎤 Le vocal marche aussi !{favorites_msg}{NAV}")
        return
    
    if state == State.DEVIS_PRESTATIONS:
        if msg == "__show__":
            send_whatsapp(phone_full, f"🔨 Décrivez les travaux et les prix :\n_Ex: Carrelage 30m² 50€, Peinture salon 800€_{NAV}")
            return
        
        # Raccourci favoris F1, F2, F3
        favs = data.get("_favorites", [])
        if msg_lower.startswith("f") and len(msg_lower) <= 3:
            try:
                fav_idx = int(msg_lower[1:]) - 1
                if 0 <= fav_idx < len(favs):
                    selected_fav = favs[fav_idx]
                    send_whatsapp(phone_full, f"✅ *{selected_fav['description']}* — {selected_fav['prix_unitaire']:.0f}€/{selected_fav['unite']}\n\nQuelle *quantité* ? _(ex: 30)_")
                    data["_pending_fav"] = selected_fav
                    conv["data"] = data
                    save_conv(phone, conv)
                    return
            except (ValueError, IndexError):
                pass
        
        # Quantité pour un favori en attente
        if data.get("_pending_fav"):
            try:
                qte = float(msg.replace(",", "."))
                fav = data["_pending_fav"]
                new_presta = {"description": fav["description"], "quantite": qte, "unite": fav["unite"], "prix_unitaire": fav["prix_unitaire"]}
                existing = data.get("prestations", [])
                existing.append(new_presta)
                data["prestations"] = existing
                data.pop("_pending_fav", None)
                total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in existing)
                lines = ["✅ C'est noté !\n"]
                for p in existing:
                    t = p["quantite"] * p["prix_unitaire"]
                    if p["quantite"] == 1 and p["unite"] in ["forfait", "u"]:
                        lines.append(f"• {p['description']} = *{fmt_amount(t)}*")
                    else:
                        lines.append(f"• {p['description']} {p['quantite']} {p['unite']} × {p['prix_unitaire']:.0f}€ = *{fmt_amount(t)}*")
                lines.append(f"━━━━━━━━━━━━")
                lines.append(f"💰 Total HT : *{fmt_amount(total_ht)}*")
                lines.append(f"\n*1.* ➕ Ajouter   *2.* ✅ OK   *3.* 🔄 Refaire")
                lines.append(NAV.strip())
                conv["data"] = data
                conv["state"] = State.DEVIS_PRESTATIONS_SUITE
                save_conv(phone, conv)
                send_whatsapp(phone_full, "\n".join(lines))
                return
            except ValueError:
                data.pop("_pending_fav", None)
                conv["data"] = data
                save_conv(phone, conv)
        
        # Parser prestations : REGEX d'abord, IA en fallback
        prestations = parse_prestations_regex(msg)
        if not prestations:
            send_whatsapp(phone_full, "⏳ _Analyse en cours..._")
            prestations = parse_prestations_ia(msg)
        if not prestations:
            send_whatsapp(phone_full, f"Je n'ai pas trouvé de prix dans votre message 🤔\n\nEssayez : _Carrelage 30m² 50€_\n💡 _Le prix en € est obligatoire !_{NAV}")
            return
        
        # Append si "Ajouter une prestation"
        existing = data.get("_prestations_precedentes", [])
        if existing:
            prestations = existing + prestations
            data.pop("_prestations_precedentes", None)
        
        data["prestations"] = prestations
        total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in prestations)
        
        lines = ["✅ C'est noté !\n"]
        for p in prestations:
            qte = p.get("quantite", 1)
            unite = p.get("unite", "u")
            pu = p.get("prix_unitaire", 0)
            desc = p.get("description", "")
            total_l = qte * pu
            if qte == 1 and unite in ["forfait", "u"]:
                lines.append(f"• {desc} = *{fmt_amount(total_l)}*")
            else:
                lines.append(f"• {desc} {qte} {unite} × {pu:.0f}€ = *{fmt_amount(total_l)}*")
        
        lines.append(f"━━━━━━━━━━━━")
        lines.append(f"💰 Total HT : *{fmt_amount(total_ht)}*")
        lines.append(f"\n*1.* ➕ Ajouter   *2.* ✅ OK   *3.* 🔄 Refaire")
        lines.append(NAV.strip())
        
        conv["data"] = data
        conv["state"] = State.DEVIS_PRESTATIONS_SUITE
        save_conv(phone, conv)
        send_whatsapp(phone_full, "\n".join(lines))
        return
    
    if state == State.DEVIS_PRESTATIONS_SUITE:
        if msg_lower in ["2", "continuer", "ok", "oui", "valider"]:
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
            send_whatsapp(phone_full, "➕ Envoyez la prestation à ajouter :\n_Ex: Plomberie forfait 500€_")
            conv["state"] = State.DEVIS_PRESTATIONS
            conv["data"]["_prestations_precedentes"] = data.get("prestations", [])
            save_conv(phone, conv)
            return
        send_whatsapp(phone_full, "*1* (ajouter) · *2* (OK) · *3* (refaire)")
        return
    
    # =========================================================================
    # RÉCAP DEVIS
    # =========================================================================
    
    if state == State.DEVIS_RECAP:
        # Sub-state: enrichissement inline
        adding = data.get("_recap_adding")
        if adding == "email":
            if "@" in msg and "." in msg:
                data["client_email"] = msg.lower().strip()
            elif msg_lower in ["non", "annuler", "retour"]:
                pass
            else:
                send_whatsapp(phone_full, "Ça ne ressemble pas à un email 🤔\nEx: *client@email.com* ou tapez *non*")
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
                    send_whatsapp(phone_full, "Entrez un pourcentage valide, ex: *10*")
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
                        send_whatsapp(phone_full, "*1* (30%) · *2* (40%) · *3* (50%) ou tapez un %")
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
            send_whatsapp(phone_full, f"""✏️ *Que modifier ?*

*1.* Nom   *2.* Tél   *3.* Email
*4.* Adresse   *5.* Projet
*6.* Prestations   *7.* Remise/Acompte
*8.* ❌ Annuler le devis{NAV}""")
            return
        if msg_lower == "3":
            # Compléter → sous-menu
            _show_completer_menu(phone, phone_full, conv)
            return
        if msg_lower == "0":
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Devis annulé." + NAV_MENU_ONLY)
            return
        send_whatsapp(phone_full, "*1* (générer) · *2* (modifier) · *3* (compléter) · *0* (annuler)")
        return
    
    if state == State.DEVIS_COMPLETER:
        # Sous-menu compléter
        completer_map = {
            "1": ("email", "📧 *Email du client ?*\n_Tapez *non* pour annuler_"),
            "2": ("adresse", "📍 *Adresse du chantier ?*\n_Tapez *non* pour annuler_"),
            "3": ("projet", "🏗️ *Nom du projet ?*\n_Ex: Rénovation salle de bain_"),
            "4": ("remise", "🏷️ *Pourcentage de remise ?*\n_Ex: 10_"),
            "5": ("acompte", "💰 *Pourcentage d'acompte ?*\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un %_"),
            "6": ("delai", "⏱️ *Délai de réalisation ?*\n_Ex: 2 semaines_"),
        }
        if msg_lower in completer_map:
            field, prompt = completer_map[msg_lower]
            data["_recap_adding"] = field
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            send_whatsapp(phone_full, prompt)
            return
        if msg_lower in ["0", "retour"]:
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            _show_recap(phone, phone_full, conv)
            return
        
        # IA PARSING : texte libre multi-champs
        # Extraire email, adresse, projet, remise, acompte, délai depuis un message libre
        updated = []
        remaining_text = msg
        
        # Email
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w{2,}', remaining_text)
        if email_match and not data.get("client_email"):
            data["client_email"] = email_match.group(0).lower()
            updated.append(f"📧 {data['client_email']}")
            remaining_text = remaining_text.replace(email_match.group(0), "").strip()
        
        # Remise (ex: "remise 20%", "20% de remise", "remise 15")
        remise_match = re.search(r'remise\s*:?\s*(\d+)\s*%?|(\d+)\s*%?\s*(?:de\s+)?remise', remaining_text, re.IGNORECASE)
        if remise_match and not data.get("remise_type"):
            val = remise_match.group(1) or remise_match.group(2)
            if val:
                data["remise_type"] = "pourcentage"
                data["remise_valeur"] = float(val)
                updated.append(f"🏷️ Remise {val}%")
                remaining_text = remaining_text[:remise_match.start()] + remaining_text[remise_match.end():]
                remaining_text = remaining_text.strip()
        
        # Acompte (ex: "acompte 30%", "30% acompte")
        acompte_match = re.search(r'acompte\s*:?\s*(\d+)\s*%?|(\d+)\s*%?\s*(?:d\'?\s*)?acompte', remaining_text, re.IGNORECASE)
        if acompte_match and not data.get("acompte_pourcentage"):
            val = acompte_match.group(1) or acompte_match.group(2)
            if val:
                data["acompte_pourcentage"] = float(val)
                updated.append(f"💰 Acompte {val}%")
                remaining_text = remaining_text[:acompte_match.start()] + remaining_text[acompte_match.end():]
                remaining_text = remaining_text.strip()
        
        # Délai (ex: "délai 2 semaines", "délai : 3 jours")
        delai_match = re.search(r'(?:^|\n)\s*d[ée]lai\s*:?\s*(.+)', remaining_text, re.IGNORECASE)
        if delai_match and not data.get("delai"):
            data["delai"] = delai_match.group(1).strip()
            updated.append(f"⏱️ {data['delai']}")
            remaining_text = remaining_text[:delai_match.start()] + remaining_text[delai_match.end():]
            remaining_text = remaining_text.strip()
        
        # Projet (ex: "projet : cuisine Reno", "projet cuisine")
        # Match only when "projet" starts the line (not in "Rue des projet")
        projet_match = re.search(r'(?:^|\n)\s*projet\s*:?\s*(.+)', remaining_text, re.IGNORECASE)
        if projet_match and not data.get("titre_projet"):
            data["titre_projet"] = projet_match.group(1).strip()
            updated.append(f"🏗️ {data['titre_projet']}")
            remaining_text = remaining_text[:projet_match.start()] + remaining_text[projet_match.end():]
            remaining_text = remaining_text.strip()
        
        # Adresse : ce qui reste (si c'est du texte et pas un numéro)
        remaining_lines = [l.strip() for l in remaining_text.split("\n") if l.strip() and not l.strip().isdigit()]
        if remaining_lines and not data.get("client_adresse"):
            # Prendre la première ligne restante comme adresse
            adresse_candidate = remaining_lines[0]
            if len(adresse_candidate) > 3 and not adresse_candidate.startswith(("oui", "non", "retour", "menu")):
                data["client_adresse"] = adresse_candidate
                updated.append(f"📍 {adresse_candidate}")
        
        if updated:
            conv["data"] = data
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            confirmation = "✅ C'est noté !\n\n" + "\n".join(updated)
            send_whatsapp(phone_full, confirmation)
            _show_recap(phone, phone_full, conv)
            return
        
        send_whatsapp(phone_full, "Tapez un numéro (1-6) ou écrivez directement :\n_Ex: email@client.com, remise 10%..._")
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
            send_whatsapp(phone_full, "❌ Devis annulé." + NAV_MENU_ONLY)
            return
        send_whatsapp(phone_full, "Tapez un numéro (1-8)")
        return
    
    if state == State.DEVIS_EMAIL:
        if msg == "__show__":
            send_whatsapp(phone_full, f"📧 Email du client ?\n_Tapez *non* si pas d'email_{NAV}")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_email"] = ""
        elif "@" in msg and "." in msg:
            data["client_email"] = msg.lower().strip()
        else:
            send_whatsapp(phone_full, "Ça ne ressemble pas à un email 🤔\nEx: *client@email.com* ou tapez *non*")
            return
        conv["data"] = data
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_ADRESSE
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ Email noté\n\n📍 Adresse du chantier ?\n_Tapez *non* si pas d'adresse_{NAV}")
        return
    
    if state == State.DEVIS_ADRESSE:
        if msg == "__show__":
            send_whatsapp(phone_full, f"📍 Adresse du chantier ?\n_Tapez *non* si pas d'adresse_{NAV}")
            return
        if msg_lower in ["non", "no", "pas", "aucun", "-", "passer"]:
            data["client_adresse"] = ""
        else:
            data["client_adresse"] = msg
        conv["data"] = data
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_PROJET
        save_conv(phone, conv)
        send_whatsapp(phone_full, f"✅ Noté\n\n📁 Nom du projet ?\n_Ex: Rénovation salle de bain_{NAV}")
        return
    
    if state == State.DEVIS_PROJET:
        if msg == "__show__":
            send_whatsapp(phone_full, f"📁 Nom du projet ?{NAV}")
            return
        data["titre_projet"] = msg
        conv["data"] = data
        if data.get("_from_recap"):
            data["_from_recap"] = False
            conv["state"] = State.DEVIS_RECAP
            save_conv(phone, conv)
            _show_recap(phone, phone_full, conv)
            return
        conv["state"] = State.DEVIS_PRESTATIONS
        save_conv(phone, conv)
        favorites_msg = _get_favorites_msg(phone, conv)
        send_whatsapp(phone_full, f"✅ *{msg}*\n\n🔨 Décrivez les travaux et les prix :\n_Ex: Carrelage 30m² 50€, Peinture salon 800€_\n🎤 Le vocal marche aussi !{favorites_msg}{NAV}")
        return
    
    if state == State.DEVIS_OPTIONS:
        if msg_lower in ["1", "remise"]:
            conv["state"] = State.DEVIS_REMISE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "🏷️ Quel *pourcentage de remise* ?\n_Ex: 10_")
            return
        if msg_lower in ["2", "acompte"]:
            conv["state"] = State.DEVIS_ACOMPTE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 Quel *pourcentage d'acompte* ?\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un %_")
            return
        if msg_lower in ["3", "delai", "délai"]:
            conv["state"] = State.DEVIS_DELAI
            save_conv(phone, conv)
            send_whatsapp(phone_full, "⏱️ Quel *délai* ?\n_Ex: 2 semaines_")
            return
        if msg_lower in ["4", "passer", "non", "rien"]:
            _show_recap(phone, phone_full, conv)
            return
        send_whatsapp(phone_full, "*1* (remise) · *2* (acompte) · *3* (délai) · *4* (passer)")
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
        send_whatsapp(phone_full, "Entrez un pourcentage valide, ex: *10*")
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
                send_whatsapp(phone_full, "*1* (30%) · *2* (40%) · *3* (50%) ou tapez un %")
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
        send_whatsapp(phone_full, "Pourcentage invalide (entre 1 et 100)")
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
    
    # =========================================================================
    # DEVIS GÉNÉRÉ - ACTIONS POST-CRÉATION
    # =========================================================================
    
    if state == State.DEVIS_GENERE:
        devis_info = data.get("devis_genere", {})
        entreprise = get_entreprise(phone)
        user_is_pro = entreprise and is_pro(entreprise)
        
        if msg_lower in ["1", "whatsapp", "envoyer"]:
            tel_client = devis_info.get("client_tel") or data.get("client_tel", "")
            conv["state"] = State.DOCS_ENVOYER_WA
            conv["data"]["send_doc"] = {**devis_info, "default_tel": tel_client, "doc_type": "devis"}
            save_conv(phone, conv)
            if tel_client:
                send_whatsapp(phone_full, f"📱 Envoyer à *{devis_info.get('client_nom', '')}* au *{tel_client}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre n°   *3.* ❌ Non")
            else:
                send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
            return
        
        if user_is_pro:
            if msg_lower in ["2", "email"]:
                email_client = devis_info.get("client_email") or data.get("client_email", "")
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                conv["data"]["send_doc"] = {**devis_info, "default_email": email_client, "doc_type": "devis"}
                save_conv(phone, conv)
                if email_client:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email_client}* ?\n\n*1.* ✍️ Avec signature\n*2.* 📄 Sans signature\n*3.* 📝 Autre email\n*4.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                    conv["state"] = State.DOCS_ENVOYER_EMAIL
                    save_conv(phone, conv)
                return
            if msg_lower in ["3", "acompte"]:
                conv["state"] = State.FACTURE_ACOMPTE_TAUX
                conv["data"]["selected_devis"] = devis_info
                save_conv(phone, conv)
                send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un %_")
                return
            if msg_lower in ["4", "nouveau"]:
                reset_conv(phone)
                handle_message(phone, "1")
                return
            if msg_lower in ["5", "menu"]:
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
        else:
            if msg_lower in ["2", "nouveau"]:
                reset_conv(phone)
                handle_message(phone, "1")
                return
            if msg_lower in ["3", "menu"]:
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
            if msg_lower in ["email"]:
                send_whatsapp(phone_full, f"🔒 L'envoi par *email* est réservé au plan Pro.\n👉 *{UPGRADE_LINK}*")
                return
            if msg_lower in ["acompte", "facture"]:
                send_whatsapp(phone_full, f"🔒 Les *factures* sont réservées au plan Pro.\n👉 *{UPGRADE_LINK}*")
                return
        
        send_whatsapp(phone_full, "Tapez un numéro pour choisir")
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
                has_finale = any(f.get("type_facture") != "acompte" for f in selected.get("factures", []))
                if has_finale:
                    send_whatsapp(phone_full, f"Ce devis a déjà une facture finale." + NAV_MENU_ONLY)
                    return
                conv["data"] = data
                conv["state"] = State.FACTURE_TYPE
                save_conv(phone, conv)
                acomptes = selected.get("factures", [])
                acomptes_payes = sum(f.get("total_ttc", 0) for f in acomptes if f.get("statut") == "payee")
                total_ttc = selected.get("total_ttc", 0)
                lines = [f"📋 *{selected.get('numero_devis', '')}* — {selected.get('client_nom', '')}", f"💰 {fmt_amount(total_ttc)} TTC\n"]
                if acomptes_payes > 0:
                    reste = total_ttc - acomptes_payes
                    lines.append(f"✅ Acomptes payés : {fmt_amount(acomptes_payes)}")
                    lines.append(f"📊 *Reste : {fmt_amount(reste)}*\n")
                lines.append("*1.* 💰 Facture d'acompte")
                lines.append("*2.* 🧾 Facture finale (solde)")
                lines.append(NAV.strip())
                send_whatsapp(phone_full, "\n".join(lines))
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, "Numéro invalide. Tapez un numéro de la liste.")
        return
    
    if state == State.FACTURE_TYPE:
        if msg_lower in ["1", "acompte"]:
            conv["state"] = State.FACTURE_ACOMPTE_TAUX
            save_conv(phone, conv)
            send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un %_")
            return
        if msg_lower in ["2", "finale", "solde"]:
            _generate_facture_finale(phone, phone_full, conv)
            return
        if msg_lower in ["3", "retour"]:
            _show_documents(phone, phone_full, conv)
            return
        send_whatsapp(phone_full, "*1* (acompte) · *2* (finale) · *retour*")
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
                send_whatsapp(phone_full, "*1* (30%) · *2* (40%) · *3* (50%) ou tapez un nombre")
                return
        if 0 < taux <= 100:
            _generate_facture_acompte(phone, phone_full, conv, taux)
            return
        send_whatsapp(phone_full, "Pourcentage invalide (1-100)")
        return
    
    if state == State.FACTURE_GENERE:
        facture_info = data.get("facture_genere", {})
        if msg_lower in ["1", "whatsapp"]:
            tel = facture_info.get("client_tel", "") or data.get("selected_devis", {}).get("telephone_client", "")
            conv["state"] = State.DOCS_ENVOYER_WA
            conv["data"]["send_doc"] = {**facture_info, "default_tel": tel}
            save_conv(phone, conv)
            if tel:
                send_whatsapp(phone_full, f"📱 Envoyer à *{tel}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre n°   *3.* ❌ Non")
            else:
                send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
            return
        if msg_lower in ["2", "email"]:
            email = facture_info.get("client_email", "") or data.get("selected_devis", {}).get("client_email", "")
            conv["state"] = State.DOCS_ENVOYER_EMAIL
            conv["data"]["send_doc"] = {**facture_info, "default_email": email, "doc_type": "facture"}
            save_conv(phone, conv)
            if email:
                send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre email   *3.* ❌ Non")
            else:
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            return
        if msg_lower in ["3", "payee", "payé", "payer"]:
            fac_id = facture_info.get("id", "")
            if fac_id and update_document_status("factures", fac_id, "payee"):
                send_whatsapp(phone_full, "✅ Facture marquée comme *payée* !" + NAV_MENU_ONLY)
            else:
                send_whatsapp(phone_full, "Erreur, réessayez 🤔" + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        if msg_lower in ["4", "menu"]:
            reset_conv(phone)
            send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
            return
        send_whatsapp(phone_full, "*1* (WhatsApp) · *2* (email) · *3* (payée) · *4* (menu)")
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
            
            entreprise = get_entreprise(phone)
            plan = get_user_plan(entreprise) if entreprise else "free"
            result = format_doc_detail(doc_entry["type"], doc_entry["data"], doc_entry.get("devis"), user_plan=plan)
            detail_text, facture_index, action_map = result
            data["facture_index"] = facture_index
            data["action_map"] = action_map
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, detail_text)
            return
        send_whatsapp(phone_full, "Numéro invalide. Tapez un numéro de la liste ou *menu*.")
        return
    
    if state == State.DOCS_DETAIL:
        doc_entry = data.get("current_doc", {})
        doc_type = doc_entry.get("type", "")
        doc = doc_entry.get("data", {})
        devis_parent = doc_entry.get("devis")
        action_map = data.get("action_map", {})
        facture_idx = data.get("facture_index", {})
        
        # Navigation vers factures liées (lettres A, B, C...)
        if msg_lower in facture_idx:
            fac_data = facture_idx[msg_lower]
            data["current_doc"] = {"type": "facture", "data": fac_data, "devis": doc}
            data["facture_index"] = {}
            data["action_map"] = {}
            conv["data"] = data
            save_conv(phone, conv)
            entreprise = get_entreprise(phone)
            plan = get_user_plan(entreprise) if entreprise else "free"
            detail_text, _, _ = format_doc_detail("facture", fac_data, doc, user_plan=plan)
            send_whatsapp(phone_full, detail_text)
            return
        
        # DEVIS actions (via action_map v9)
        if doc_type == "devis":
            action = action_map.get(msg_lower, "")
            
            if action == "whatsapp":
                tel = doc.get("telephone_client", "")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_devis", ""), "client_nom": doc.get("client_nom", ""), "default_tel": tel, "doc_type": "devis", "id": doc.get("id", "")}
                save_conv(phone, conv)
                if tel:
                    send_whatsapp(phone_full, f"📱 Envoyer à *{doc.get('client_nom', '')}* au *{tel}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre n°   *3.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
                return
            
            if action == "email":
                entreprise = get_entreprise(phone)
                if entreprise and not is_pro(entreprise):
                    send_whatsapp(phone_full, f"🔒 L'envoi par *email* est réservé au plan Pro.\n👉 *{UPGRADE_LINK}*")
                    return
                email = doc.get("client_email", "")
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_devis", ""), "id": doc.get("id", ""), "client_nom": doc.get("client_nom", ""), "default_email": email, "doc_type": "devis", "total_ttc": doc.get("total_ttc", 0), "titre_projet": doc.get("titre_projet", "")}
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✍️ Avec signature\n*2.* 📄 Sans signature\n*3.* 📝 Autre email\n*4.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                    conv["state"] = State.DOCS_ENVOYER_EMAIL
                    save_conv(phone, conv)
                return
            
            if action == "facture_acompte":
                entreprise = get_entreprise(phone)
                if entreprise and not is_pro(entreprise):
                    send_whatsapp(phone_full, UPGRADE_MSG_FACTURES)
                    return
                conv["state"] = State.FACTURE_ACOMPTE_TAUX
                conv["data"]["selected_devis"] = doc
                save_conv(phone, conv)
                send_whatsapp(phone_full, "💰 *Facture d'acompte*\n\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un %_")
                return
            
            if action == "facture_finale":
                entreprise = get_entreprise(phone)
                if entreprise and not is_pro(entreprise):
                    send_whatsapp(phone_full, UPGRADE_MSG_FACTURES)
                    return
                conv["data"]["selected_devis"] = doc
                save_conv(phone, conv)
                _generate_facture_finale(phone, phone_full, conv)
                return
            
            if action == "facturer_locked":
                send_whatsapp(phone_full, UPGRADE_MSG_FACTURES)
                return
            
            if action == "modifier":
                # TODO: implement edit from docs
                send_whatsapp(phone_full, "✏️ Pour modifier, créez un nouveau devis via *Dupliquer* (tapez *4* au menu)." + NAV_MENU_ONLY)
                return
            
            if action == "supprimer":
                conv["state"] = State.DOCS_CONFIRMER_SUPPR
                conv["data"]["suppr_doc"] = {"type": "devis", "id": doc.get("id", ""), "numero": doc.get("numero_devis", "")}
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"🗑️ Supprimer le devis *{doc.get('client_nom', '')}* ?\n\n⚠️ Les factures liées seront aussi supprimées.\n\n*1.* ✅ Oui   *2.* ❌ Non")
                return
            
            if msg_lower in ["retour"]:
                _show_documents(phone, phone_full, conv)
                return
        
        # FACTURE actions
        elif doc_type == "facture":
            is_paid = doc.get("statut") in ("payee", "paye")
            
            if msg_lower == "1":
                tel = doc.get("client_telephone", "") or (devis_parent or {}).get("telephone_client", "")
                conv["state"] = State.DOCS_ENVOYER_WA
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_facture", ""), "client_nom": doc.get("client_nom", ""), "default_tel": tel, "doc_type": "facture"}
                save_conv(phone, conv)
                if tel:
                    send_whatsapp(phone_full, f"📱 Envoyer à *{tel}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre n°   *3.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📱 Entrez le numéro du client :")
                return
            
            if msg_lower == "2":
                email = doc.get("client_email", "") or (devis_parent or {}).get("client_email", "")
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                conv["data"]["send_doc"] = {"pdf_url": doc.get("pdf_url", ""), "numero": doc.get("numero_facture", ""), "client_nom": doc.get("client_nom", ""), "default_email": email, "doc_type": "facture", "total_ttc": doc.get("total_ttc", 0)}
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre email   *3.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
                return
            
            if msg_lower == "3":
                if is_paid:
                    # 3 = supprimer (paid factures have no "marquer payée")
                    conv["state"] = State.DOCS_CONFIRMER_SUPPR
                    conv["data"]["suppr_doc"] = {"type": "facture", "id": doc.get("id", ""), "numero": doc.get("numero_facture", "")}
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, f"🗑️ Supprimer la facture *{doc.get('numero_facture', '')}* ?\n\n*1.* ✅ Oui   *2.* ❌ Non")
                else:
                    # 3 = marquer payée
                    fac_id = doc.get("id", "")
                    if fac_id and update_document_status("factures", fac_id, "payee"):
                        send_whatsapp(phone_full, "✅ Facture marquée comme *payée* !\n\n*1.* 📂 Retour documents\n*2.* 🏠 Menu")
                        conv["state"] = State.MENU
                        save_conv(phone, conv)
                    else:
                        send_whatsapp(phone_full, "Erreur, réessayez 🤔" + NAV_MENU_ONLY)
                    return
                return
            
            if msg_lower == "4" and not is_paid:
                conv["state"] = State.DOCS_CONFIRMER_SUPPR
                conv["data"]["suppr_doc"] = {"type": "facture", "id": doc.get("id", ""), "numero": doc.get("numero_facture", "")}
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"🗑️ Supprimer la facture *{doc.get('numero_facture', '')}* ?\n\n*1.* ✅ Oui   *2.* ❌ Non")
                return
            
            if msg_lower in ["retour"]:
                if devis_parent:
                    data["current_doc"] = {"type": "devis", "data": devis_parent}
                    conv["data"] = data
                    conv["state"] = State.DOCS_DETAIL
                    save_conv(phone, conv)
                    entreprise = get_entreprise(phone)
                    plan = get_user_plan(entreprise) if entreprise else "free"
                    detail_text, fac_idx, act_map = format_doc_detail("devis", devis_parent, user_plan=plan)
                    data["facture_index"] = fac_idx
                    data["action_map"] = act_map
                    conv["data"] = data
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, detail_text)
                else:
                    _show_documents(phone, phone_full, conv)
                return
        
        send_whatsapp(phone_full, "Tapez un numéro d'action ou *retour*")
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
            data["send_doc"]["default_tel"] = ""
            conv["data"] = data
            save_conv(phone, conv)
            return
        elif msg_lower in ["3", "non", "annuler"]:
            # Après annulation, proposer la suite
            send_whatsapp(phone_full, "❌ Envoi annulé." + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        else:
            tel = re.sub(r'[^0-9+]', '', msg)
            if len(tel) < 10:
                send_whatsapp(phone_full, "Numéro incorrect 🤔 — 10 chiffres minimum")
                return
        
        # Envoyer le document
        pdf_url = send_doc.get("pdf_url", "")
        numero = send_doc.get("numero", send_doc.get("numero_devis", ""))
        client = send_doc.get("client_nom", "")
        doc_type = send_doc.get("doc_type", "devis")
        
        if not pdf_url:
            send_whatsapp(phone_full, "Hmm, le PDF n'a pas été trouvé 🤔" + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        
        tel_full = f"+{tel}" if not tel.startswith("+") else tel
        tel_wa = f"whatsapp:{tel_full}"
        
        send_whatsapp_document(tel_wa, pdf_url, f"📄 {'Devis' if doc_type == 'devis' else 'Facture'} {numero}")
        
        # Mettre à jour statut
        doc_id = send_doc.get("id", "")
        if doc_id:
            table = "devis" if doc_type == "devis" else "factures"
            update_document_status(table, doc_id, "envoye" if doc_type == "devis" else "envoyee")
        
        # Message post-envoi avec suite logique
        next_actions = [f"✅ {'Devis' if doc_type == 'devis' else 'Facture'} envoyé à *{client}* par WhatsApp !\n"]
        if doc_type == "devis":
            next_actions.append("*1.* 📧 Envoyer aussi par email")
            next_actions.append("*2.* 📝 Nouveau devis")
            next_actions.append("*3.* 🏠 Menu")
        else:
            next_actions.append("*1.* 📧 Envoyer aussi par email")
            next_actions.append("*2.* 🏠 Menu")
        
        send_whatsapp(phone_full, "\n".join(next_actions))
        
        # État dédié pour gérer les actions post-envoi
        conv["state"] = State.POST_ENVOI
        conv["data"]["_post_send"] = send_doc
        save_conv(phone, conv)
        return
    
    # =========================================================================
    # POST-ENVOI (après envoi WhatsApp réussi)
    # =========================================================================
    
    if state == State.POST_ENVOI:
        post_doc = data.get("_post_send", {})
        doc_type = post_doc.get("doc_type", "devis")
        
        if msg_lower == "1":
            # Envoyer aussi par email
            email = post_doc.get("client_email", post_doc.get("default_email", ""))
            conv["data"]["send_doc"] = post_doc
            if doc_type == "devis":
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✍️ Avec signature\n*2.* 📄 Sans signature\n*3.* 📝 Autre email\n*4.* ❌ Non")
                else:
                    conv["state"] = State.DOCS_ENVOYER_EMAIL
                    save_conv(phone, conv)
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            else:
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                conv["data"]["send_doc"]["default_email"] = email
                save_conv(phone, conv)
                if email:
                    send_whatsapp(phone_full, f"📧 Envoyer à *{email}* ?\n\n*1.* ✅ Oui   *2.* 📝 Autre email   *3.* ❌ Non")
                else:
                    send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            return
        
        if doc_type == "devis":
            if msg_lower == "2":
                reset_conv(phone)
                handle_message(phone, "1")  # Nouveau devis
                return
            if msg_lower == "3":
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
        else:
            if msg_lower == "2":
                reset_conv(phone)
                send_whatsapp_template(phone_full, TEMPLATE_MENU_SID)
                return
        
        send_whatsapp(phone_full, "Tapez un numéro pour choisir" + NAV_MENU_ONLY)
        return
    
    # =========================================================================
    # SIGNATURE CHOIX (email devis)
    # =========================================================================
    
    if state == State.DOCS_SIGNATURE_CHOIX:
        send_doc = data.get("send_doc", {})
        default_email = send_doc.get("default_email", "")
        
        if msg_lower in ["1", "signature"]:
            send_doc["avec_signature"] = True
            conv["data"]["send_doc"] = send_doc
            if default_email:
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                save_conv(phone, conv)
                _send_email_action(phone, phone_full, conv, default_email, avec_signature=True)
            else:
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                save_conv(phone, conv)
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            return
        
        if msg_lower in ["2", "sans"]:
            send_doc["avec_signature"] = False
            conv["data"]["send_doc"] = send_doc
            if default_email:
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                save_conv(phone, conv)
                _send_email_action(phone, phone_full, conv, default_email, avec_signature=False)
            else:
                conv["state"] = State.DOCS_ENVOYER_EMAIL
                save_conv(phone, conv)
                send_whatsapp(phone_full, "📧 Entrez l'email du client :")
            return
        
        if msg_lower in ["3", "autre"]:
            send_whatsapp(phone_full, "📧 Entrez l'email :")
            send_doc["default_email"] = ""
            conv["data"]["send_doc"] = send_doc
            conv["state"] = State.DOCS_ENVOYER_EMAIL
            save_conv(phone, conv)
            return
        
        if msg_lower in ["4", "non", "annuler"]:
            send_whatsapp(phone_full, "❌ Annulé." + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        
        send_whatsapp(phone_full, "*1* (avec signature) · *2* (sans) · *3* (autre email) · *4* (annuler)")
        return
    
    # =========================================================================
    # ENVOI EMAIL
    # =========================================================================
    
    if state == State.DOCS_ENVOYER_EMAIL:
        send_doc = data.get("send_doc", {})
        default_email = send_doc.get("default_email", "")
        
        if msg_lower in ["1", "oui"] and default_email:
            avec_signature = send_doc.get("avec_signature", False)
            _send_email_action(phone, phone_full, conv, default_email, avec_signature=avec_signature)
            return
        
        if msg_lower in ["2", "autre"]:
            send_whatsapp(phone_full, "📧 Entrez le nouvel email :")
            data["send_doc"]["default_email"] = ""
            conv["data"] = data
            save_conv(phone, conv)
            return
        
        if msg_lower in ["3", "non", "annuler"]:
            send_whatsapp(phone_full, "❌ Annulé." + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        
        # Email saisi directement
        if "@" in msg and "." in msg:
            doc_type = send_doc.get("doc_type", "devis")
            avec_signature = send_doc.get("avec_signature", False)
            
            if doc_type == "devis" and not send_doc.get("_signature_asked"):
                conv["data"]["send_doc"]["default_email"] = msg.lower().strip()
                conv["data"]["send_doc"]["_signature_asked"] = True
                conv["state"] = State.DOCS_SIGNATURE_CHOIX
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"📧 *{msg}*\n\n*1.* ✍️ Avec signature\n*2.* 📄 Sans signature\n*3.* ❌ Annuler")
                return
            
            _send_email_action(phone, phone_full, conv, msg.lower().strip(), avec_signature=avec_signature)
            return
        
        send_whatsapp(phone_full, "Ça ne ressemble pas à un email 🤔\nRéessayez ou tapez *annuler*")
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
                if doc_type == "devis" and supabase_client:
                    try:
                        supabase_client.table("factures").update({"deleted_at": datetime.now().isoformat()}).eq("devis_id", doc_id).execute()
                    except:
                        pass
                send_whatsapp(phone_full, f"✅ Supprimé !" + NAV_MENU_ONLY)
            else:
                send_whatsapp(phone_full, "Erreur de suppression 🤔" + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        if msg_lower in ["2", "non", "annuler"]:
            send_whatsapp(phone_full, "↩️ Suppression annulée." + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        send_whatsapp(phone_full, "*1* (supprimer) · *2* (annuler)")
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
                send_whatsapp(phone_full, f"📋 *Dupliquer*\n\n*1.* 👤 Même client ({client})\n*2.* 🆕 Nouveau client{NAV}")
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, f"Tapez un numéro (1-{len(options)}) ou *menu*")
        return
    
    if state == State.DEVIS_DUPLICATE_CLIENT:
        source = data.get("duplicate_source", {})
        if not source:
            send_whatsapp(phone_full, "Erreur 🤔" + NAV_MENU_ONLY)
            return
        prestations_raw = source.get("prestations", "[]")
        if isinstance(prestations_raw, str):
            try:
                prestations_parsed = json.loads(prestations_raw)
            except:
                prestations_parsed = []
        else:
            prestations_parsed = prestations_raw
        prestations_internes = []
        for p in prestations_parsed:
            prestations_internes.append({
                "description": p.get("description", ""),
                "quantite": p.get("quantite", 1),
                "unite": p.get("unite", "u"),
                "prix_unitaire": p.get("prix_unitaire_ht") or p.get("prix_unitaire", 0),
            })
        
        if msg_lower in ["1", "meme", "même"]:
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
            lines = [f"📋 *Devis dupliqué !*\n", f"👤 {source.get('client_nom', '')}"]
            for p in prestations_internes:
                t = p["quantite"] * p["prix_unitaire"]
                lines.append(f"• {p['description']} = {fmt_amount(t)}")
            lines.append(f"\n💰 *Total HT : {fmt_amount(total_ht)}*")
            lines.append(f"\n*1.* ✅ OK   *2.* ✏️ Modifier   *3.* ❌ Annuler")
            conv["state"] = State.DEVIS_PRESTATIONS_SUITE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        if msg_lower in ["2", "nouveau", "new"]:
            conv["data"] = {"prestations": prestations_internes, "_from_duplicate": True}
            conv["state"] = State.DEVIS_NOM
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"👤 Nom du nouveau client ?{NAV}")
            return
        send_whatsapp(phone_full, "*1* (même client) · *2* (nouveau)")
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
                emoji = "🔴" if selected["urgency"] == "red" else "🟡"
                send_whatsapp(phone_full, f"""{emoji} *{type_label} — {selected['client_nom']}*
{fmt_amount(selected['total_ttc'])} · {selected['days_overdue']} jours de retard

Comment relancer ?

*1.* 📱 WhatsApp   *2.* 📧 Email{NAV}""")
                return
        except ValueError:
            pass
        send_whatsapp(phone_full, f"Tapez un numéro (1-{len(items)}) ou *menu*")
        return
    
    if state == State.RELANCE_ACTION:
        selected = data.get("relance_selected", {})
        if not selected:
            reset_conv(phone)
            send_whatsapp(phone_full, "Erreur 🤔" + NAV_MENU_ONLY)
            return
        type_label = "facture" if selected["type"] == "facture" else "devis"
        client = selected["client_nom"]
        montant = selected["total_ttc"]
        numero = selected["numero"]
        jours = selected["days_overdue"]
        
        if jours > 30:
            template_msg = f"Bonjour,\n\nSauf erreur de ma part, la {type_label} {numero} d'un montant de {montant:.2f}€ reste impayée depuis {jours} jours.\n\nMerci de procéder au règlement dans les plus brefs délais.\n\nCordialement"
        else:
            template_msg = f"Bonjour,\n\nPetit rappel concernant la {type_label} {numero} ({montant:.2f}€). N'hésitez pas si vous avez des questions.\n\nCordialement"
        
        if msg_lower in ["1", "whatsapp"]:
            tel = selected.get("tel", "")
            if tel:
                conv["data"]["relance_msg"] = template_msg
                conv["data"]["relance_method"] = "whatsapp"
                conv["data"]["relance_tel"] = tel
                conv["state"] = State.RELANCE_MSG
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"📱 *Relance → {client}*\n\n_{template_msg}_\n\n*1.* ✅ Envoyer   *2.* ✏️ Modifier   *3.* ❌ Annuler")
                return
            else:
                send_whatsapp(phone_full, f"Pas de numéro pour {client} 🤔\nTapez *2* pour relancer par email")
                return
        
        if msg_lower in ["2", "email"]:
            email = selected.get("email", "")
            if email:
                conv["data"]["relance_msg"] = template_msg
                conv["data"]["relance_method"] = "email"
                conv["data"]["relance_email"] = email
                conv["state"] = State.RELANCE_MSG
                save_conv(phone, conv)
                send_whatsapp(phone_full, f"📧 *Relance → {client}* ({email})\n\n_{template_msg}_\n\n*1.* ✅ Envoyer   *2.* ✏️ Modifier   *3.* ❌ Annuler")
                return
            else:
                send_whatsapp(phone_full, f"Pas d'email pour {client} 🤔\nTapez *1* pour relancer par WhatsApp")
                return
        
        if msg_lower in ["retour"]:
            conv["state"] = State.RELANCE_LISTE
            save_conv(phone, conv)
            items = data.get("relance_items", [])
            lines = ["🔔 *Relances*\n"]
            for i, item in enumerate(items, 1):
                emoji = "🔴" if item["urgency"] == "red" else "🟡"
                tl = "Facture" if item["type"] == "facture" else "Devis"
                lines.append(f"*{i}.* {emoji} {tl} · {item['client_nom']} · {fmt_amount(item['total_ttc'])} · {item['days_overdue']}j")
            lines.append(NAV.strip())
            send_whatsapp(phone_full, "\n".join(lines))
            return
        
        send_whatsapp(phone_full, "*1* (WhatsApp) · *2* (email) · *retour*")
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
                    send_whatsapp(phone_full, f"✅ Relance envoyée à *{client}* !" + NAV_MENU_ONLY)
                else:
                    send_whatsapp(phone_full, "Numéro manquant 🤔" + NAV_MENU_ONLY)
            elif method == "email":
                email = data.get("relance_email", "")
                send_whatsapp(phone_full, f"✅ Relance envoyée à *{client}* ({email}) !" + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        
        if msg_lower in ["2", "modifier"]:
            send_whatsapp(phone_full, "✏️ Envoyez votre message personnalisé :")
            conv["data"]["_editing_relance"] = True
            save_conv(phone, conv)
            return
        
        if data.get("_editing_relance"):
            data["relance_msg"] = msg
            data.pop("_editing_relance", None)
            conv["data"] = data
            save_conv(phone, conv)
            send_whatsapp(phone_full, f"✅ Message mis à jour.\n\n*1.* ✅ Envoyer   *3.* ❌ Annuler")
            return
        
        if msg_lower in ["3", "annuler"]:
            reset_conv(phone)
            send_whatsapp(phone_full, "❌ Relance annulée." + NAV_MENU_ONLY)
            return
        
        send_whatsapp(phone_full, "*1* (envoyer) · *2* (modifier) · *3* (annuler)")
        return
    
    # =========================================================================
    # COMBO POST-DEVIS
    # =========================================================================
    
    if state == State.COMBO_CONFIRM:
        combo_devis = data.get("combo_devis", {})
        taux = data.get("combo_taux", 30)
        
        if msg_lower in ["1", "ok", "oui", "go", "lancer"]:
            send_whatsapp(phone_full, "🚀 *En cours...*")
            tel = combo_devis.get("client_tel", "")
            pdf_url = combo_devis.get("pdf_url", "")
            client = combo_devis.get("client_nom", "")
            numero = combo_devis.get("numero_devis", "")
            if tel and pdf_url:
                tel_full_client = f"+{tel}" if not tel.startswith("+") else tel
                if not tel_full_client.startswith("whatsapp:"):
                    tel_full_client = f"whatsapp:{tel_full_client}"
                send_whatsapp_document(tel_full_client, pdf_url, f"📄 Devis {numero}")
                send_whatsapp(phone_full, f"✅ Devis envoyé à {client}")
            email = combo_devis.get("client_email", "")
            if email:
                entreprise = get_entreprise(phone)
                if entreprise and supabase_client:
                    try:
                        supabase_client.table("email_queue").insert({
                            "entreprise_id": entreprise["id"],
                            "to_email": email,
                            "type": "devis",
                            "doc_id": combo_devis.get("id", ""),
                            "status": "pending",
                        }).execute()
                        send_whatsapp(phone_full, f"✅ Email envoyé à {email}")
                    except Exception as e:
                        logger.error(f"Erreur email combo: {e}")
            conv["state"] = State.FACTURE_ACOMPTE_TAUX
            conv["data"]["selected_devis"] = combo_devis
            save_conv(phone, conv)
            handle_message(phone, str(taux))
            return
        
        if msg_lower in ["2", "modifier", "taux"]:
            send_whatsapp(phone_full, "📊 Quel taux ?\n*1.* 30%  *2.* 40%  *3.* 50%  _ou tapez un nombre_")
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
                    send_whatsapp(phone_full, f"✅ Acompte : *{new_taux}%*\n\n*1.* ✅ Lancer   *3.* ❌ Annuler")
                    return
            except ValueError:
                pass
            send_whatsapp(phone_full, "Pourcentage valide (1-100)")
            return
        
        if msg_lower in ["3", "annuler"]:
            conv["state"] = State.DEVIS_GENERE
            save_conv(phone, conv)
            send_whatsapp(phone_full, "❌ Annulé. Tapez un numéro ou *menu*")
            return
        
        send_whatsapp(phone_full, "*1* (lancer) · *2* (modifier taux) · *3* (annuler)")
        return
    
    # =========================================================================
    # ÉTAT INCONNU
    # =========================================================================
    send_whatsapp(phone_full, "Je n'ai pas compris 🤔" + NAV_MENU_ONLY)


# =============================================================================
# FONCTIONS HELPER
# =============================================================================

def _get_favorites_msg(phone: str, conv: Dict) -> str:
    """Retourne le message de favoris si Pro"""
    entreprise = get_entreprise(phone)
    if not entreprise or not is_pro(entreprise):
        return ""
    favs = get_frequent_prestations(entreprise["id"])
    if not favs:
        return ""
    fav_lines = ["\n\n💡 *Vos prestations habituelles :*"]
    for i, f in enumerate(favs[:3], 1):
        fav_lines.append(f"*F{i}.* {f['description']} — {f['prix_unitaire']:.0f}€/{f['unite']}")
    fav_lines.append("_Tapez F1, F2... pour ajouter_")
    conv["data"]["_favorites"] = favs[:3]
    save_conv(phone, conv)
    return "\n".join(fav_lines)


def _show_completer_menu(phone: str, phone_full: str, conv: Dict):
    """Affiche le sous-menu Compléter (v9)"""
    data = conv.get("data", {})
    lines = ["➕ *Compléter le devis*\n"]
    num = 1
    if not data.get("client_email"):
        lines.append(f"*{num}.* 📧 Email")
        num += 1
    if not data.get("client_adresse"):
        lines.append(f"*{num}.* 📍 Adresse")
        num += 1
    if not data.get("titre_projet"):
        lines.append(f"*{num}.* 🏗️ Projet")
        num += 1
    if not data.get("remise_type"):
        lines.append(f"*{num}.* 🏷️ Remise")
        num += 1
    if not data.get("acompte_pourcentage"):
        lines.append(f"*{num}.* 💰 Acompte")
        num += 1
    if not data.get("delai"):
        lines.append(f"*{num}.* ⏱️ Délai")
        num += 1
    
    if num == 1:
        # Tout est déjà rempli
        send_whatsapp(phone_full, "✅ Devis déjà complet !" + NAV)
        conv["state"] = State.DEVIS_RECAP
        save_conv(phone, conv)
        _show_recap(phone, phone_full, conv)
        return
    
    lines.append(f"\n*0.* ↩️ Retour au récap")
    
    # Re-numéroter proprement pour le sous-menu
    # On utilise le mapping standard 1-6
    conv["state"] = State.DEVIS_COMPLETER
    save_conv(phone, conv)
    
    # Rebuild avec numérotation fixe pour simplifier le handler
    fixed_lines = ["➕ *Compléter le devis*\n"]
    idx = 1
    options = []
    if not data.get("client_email"):
        fixed_lines.append(f"*{idx}.* 📧 Email")
        options.append(("email", idx))
        idx += 1
    if not data.get("client_adresse"):
        fixed_lines.append(f"*{idx}.* 📍 Adresse")
        options.append(("adresse", idx))
        idx += 1
    if not data.get("titre_projet"):
        fixed_lines.append(f"*{idx}.* 🏗️ Projet")
        options.append(("projet", idx))
        idx += 1
    if not data.get("remise_type"):
        fixed_lines.append(f"*{idx}.* 🏷️ Remise")
        options.append(("remise", idx))
        idx += 1
    if not data.get("acompte_pourcentage"):
        fixed_lines.append(f"*{idx}.* 💰 Acompte")
        options.append(("acompte", idx))
        idx += 1
    if not data.get("delai"):
        fixed_lines.append(f"*{idx}.* ⏱️ Délai")
        options.append(("delai", idx))
        idx += 1
    fixed_lines.append(f"\n*0.* ↩️ Retour")
    
    # Stocker le mapping
    completer_map = {}
    for field, num in options:
        completer_map[str(num)] = field
    conv["data"]["_completer_map"] = completer_map
    save_conv(phone, conv)
    
    send_whatsapp(phone_full, "\n".join(fixed_lines))


def _show_documents(phone: str, phone_full: str, conv: Dict):
    """Affiche la liste des documents v9"""
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "Configurez d'abord votre profil sur *vocario.fr* 🏗️" + NAV_MENU_ONLY)
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
    """Affiche le récap compact v9"""
    data = conv.get("data", {})
    prestations = data.get("prestations", [])
    total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in prestations)
    
    remise_type = data.get("remise_type")
    remise_valeur = data.get("remise_valeur", 0)
    remise_montant = 0
    if remise_type == "pourcentage" and remise_valeur > 0:
        remise_montant = total_ht * (remise_valeur / 100)
    total_ht_apres_remise = total_ht - remise_montant
    
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
    
    # Header compact
    lines = ["📋 *Récap devis*\n"]
    
    # Client + tel sur une ligne
    client_line = f"👤 *{data.get('client_nom', '')}*"
    if data.get("client_tel"):
        client_line += f" · 📞 {data['client_tel']}"
    lines.append(client_line)
    
    # Infos optionnelles (seulement si remplies)
    if data.get("client_email"):
        lines.append(f"📧 {data['client_email']}")
    if data.get("client_adresse"):
        lines.append(f"📍 {data['client_adresse']}")
    if data.get("titre_projet"):
        lines.append(f"🏗️ {data['titre_projet']}")
    
    # Prestations
    for p in prestations:
        qte = p.get("quantite", 1)
        unite = p.get("unite", "u")
        pu = p.get("prix_unitaire", 0)
        desc = p.get("description", "")
        total_l = qte * pu
        if qte == 1 and unite in ["forfait", "u"]:
            lines.append(f"🔨 {desc} = *{fmt_amount(total_l)}*")
        else:
            lines.append(f"🔨 {desc} {qte} {unite} × {pu:.0f}€ = *{fmt_amount(total_l)}*")
    
    lines.append("━━━━━━━━━━━━")
    
    # Montants
    if remise_montant > 0:
        lines.append(f"🏷️ Remise {remise_valeur}% : -{fmt_amount(remise_montant)}")
    
    if tva_taux > 0:
        lines.append(f"💰 *Total TTC : {fmt_amount(total_ttc)}*")
    else:
        lines.append(f"💰 *Total : {fmt_amount(total_ttc)}* _(TVA non applicable)_")
    
    if acompte > 0:
        lines.append(f"📅 Acompte {acompte}% : {fmt_amount(acompte_montant)}")
    if data.get("delai"):
        lines.append(f"⏱️ Délai : {data['delai']}")
    
    # Actions v9 : compactes
    lines.append("\n*1.* ✅ Générer le devis")
    lines.append("*2.* ✏️ Modifier")
    
    # Compter les champs optionnels manquants
    missing = []
    if not data.get("client_email"):
        missing.append("email")
    if not data.get("client_adresse"):
        missing.append("adresse")
    if not data.get("titre_projet"):
        missing.append("projet")
    if not data.get("remise_type"):
        missing.append("remise")
    if not data.get("acompte_pourcentage"):
        missing.append("acompte")
    if not data.get("delai"):
        missing.append("délai")
    
    if missing:
        lines.append(f"*3.* ➕ Compléter ({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})")
    
    lines.append("*0.* ❌ Annuler")
    lines.append(NAV.strip())
    
    conv["state"] = State.DEVIS_RECAP
    save_conv(phone, conv)
    send_whatsapp(phone_full, "\n".join(lines))


def _generate_devis(phone: str, phone_full: str, conv: Dict):
    """Génère le devis PDF"""
    data = conv.get("data", {})
    send_whatsapp(phone_full, "⏳ _Génération en cours..._")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)
        return
    
    try:
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
        
        entreprise_model = Entreprise(
            nom=entreprise.get("nom", ""), gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""), adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""), tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""), logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux, mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            conditions_paiement=entreprise.get("conditions_paiement", "30% à la commande, solde à réception"),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        
        client_model = Client(
            nom=data.get("client_nom", ""), adresse=data.get("client_adresse", ""),
            tel=data.get("client_tel", ""), email=data.get("client_email", ""),
        )
        
        prestations_for_db = []
        for p in data.get("prestations", []):
            prestations_for_db.append({
                "description": p.get("description", ""), "quantite": p.get("quantite", 1),
                "unite": p.get("unite", "u"), "prix_unitaire_ht": p.get("prix_unitaire", 0),
                "prix_unitaire": p.get("prix_unitaire", 0), "tva_taux": tva_taux,
            })
        
        total_ht = sum(p.get("quantite", 1) * p.get("prix_unitaire", 0) for p in data.get("prestations", []))
        remise_type = data.get("remise_type")
        remise_valeur = data.get("remise_valeur", 0)
        remise = total_ht * (remise_valeur / 100) if remise_type == "pourcentage" and remise_valeur > 0 else 0
        total_ht_final = total_ht - remise
        total_tva = total_ht_final * (tva_taux / 100)
        total_ttc = total_ht_final + total_tva
        
        # Auto-générer le titre du projet si pas renseigné
        titre = data.get("titre_projet") or auto_titre_projet(data.get("prestations", []))
        data["titre_projet"] = titre
        
        saved = save_devis_to_dashboard(
            entreprise_id=entreprise["id"], numero_devis="TEMP",
            client_nom=data.get("client_nom", ""), client_email=data.get("client_email"),
            client_telephone=data.get("client_tel"), titre_projet=titre,
            prestations=prestations_for_db, total_ht=total_ht_final, total_ttc=total_ttc,
            pdf_url=None, word_url=None, remise_type=remise_type,
            remise_value=remise_valeur, delai=data.get("delai"),
        )
        
        if not saved:
            send_whatsapp(phone_full, "Erreur lors de la création 🤔" + NAV_MENU_ONLY)
            reset_conv(phone)
            return
        
        numero_devis = saved.get("numero_devis", f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}")
        devis_db_id = saved.get("id", "")
        
        devis_request = DevisRequest(
            entreprise=entreprise_model, client=client_model,
            prestations=prestations_for_api, tva_taux=tva_taux,
            conditions_paiement=entreprise.get("conditions_paiement", "30% à la commande, solde à réception"),
            delai_realisation=data.get("delai", "À définir"),
            validite_jours=int(entreprise.get("delai_validite", 30) or 30),
            remise_type=remise_type, remise_valeur=remise_valeur or 0,
            acompte_pourcentage=data.get("acompte_pourcentage", 0),
            numero_devis=numero_devis,
        )
        
        filepath_pdf, _, total_ht_calc, total_ttc_calc = generer_pdf_devis(devis_request, numero_devis_force=numero_devis)
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis}.pdf")
        
        word_url = None
        if is_pro(entreprise):
            filepath_word, _, _, _ = generer_word_devis(devis_request, numero_devis_force=numero_devis)
            word_url = upload_to_supabase(filepath_word, f"{numero_devis}.docx")
        
        if supabase_client and devis_db_id:
            try:
                supabase_client.table("devis").update({
                    "numero_devis": numero_devis, "pdf_url": pdf_url,
                    "word_url": word_url, "total_ht": total_ht_calc, "total_ttc": total_ttc_calc,
                }).eq("id", devis_db_id).execute()
            except Exception as e:
                logger.error(f"Erreur update devis: {e}")
        
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"📄 Devis {numero_devis}")
        
        user_is_user_is_pro = is_pro(entreprise)
        tel_client = data.get("client_tel", "")
        projet = data.get("titre_projet", "")
        
        # Astuce express
        express_tip = ""
        if not data.get("_from_express") and not data.get("_from_duplicate"):
            express_tip = "\n\n💡 _Astuce : envoyez tout en 1 message !_\n→ _Dupont 06... carrelage 30m² 50€_"
        
        # Message de succès v9
        header = f"✅ *Devis prêt !*\n\n📋 {numero_devis}"
        if projet:
            header += f" — {projet}"
        header += f"\n👤 {data.get('client_nom', '')} · 💰 *{fmt_amount(total_ttc_calc)} TTC*"
        header += "\n\nComment on l'envoie ?"
        
        if user_is_pro:
            actions = f"\n*1.* 📱 WhatsApp"
            if tel_client:
                actions += f" → {tel_client}"
            actions += "\n*2.* 📧 Email + signature ✍️"
            actions += "\n*3.* 💰 Facture d'acompte"
            actions += f"\n*4.* 📝 Nouveau devis · *5.* 🏠 Menu{express_tip}"
        else:
            _, _, remaining = check_can_create_devis(entreprise)
            nudge = ""
            if remaining == 1:
                nudge = "\n⚠️ _Dernier devis gratuit ! Tapez *upgrade*_"
            elif remaining == 0:
                nudge = "\n🔒 _Limite atteinte. Tapez *upgrade*_"
            else:
                nudge = f"\n📊 _{remaining} devis restant(s) ce mois_"
            actions = f"\n*1.* 📱 WhatsApp"
            if tel_client:
                actions += f" → {tel_client}"
            actions += f"\n*2.* 📝 Nouveau devis · *3.* 🏠 Menu{nudge}{express_tip}"
        
        send_whatsapp(phone_full, header + actions)
        
        conv["state"] = State.DEVIS_GENERE
        conv["data"]["devis_genere"] = {
            "id": devis_db_id, "numero_devis": numero_devis,
            "client_nom": data.get("client_nom", ""), "client_tel": data.get("client_tel", ""),
            "client_email": data.get("client_email", ""), "total_ttc": total_ttc_calc,
            "total_ht": total_ht_calc, "pdf_url": pdf_url, "word_url": word_url,
            "titre_projet": data.get("titre_projet", ""),
        }
        save_conv(phone, conv)
        
    except Exception as e:
        logger.error(f"Erreur génération devis: {e}")
        traceback.print_exc()
        send_whatsapp(phone_full, f"Erreur technique 🤔\n_{str(e)[:80]}_" + NAV_MENU_ONLY)
        reset_conv(phone)


def _generate_facture_acompte(phone: str, phone_full: str, conv: Dict, taux: float):
    """Génère une facture d'acompte"""
    data = conv.get("data", {})
    devis = data.get("selected_devis", {})
    send_whatsapp(phone_full, f"⏳ _Facture acompte {taux}%..._")
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)
        return
    try:
        tva_taux = float(entreprise.get("tva_taux", 20) or 20)
        total_ht_devis = float(devis.get("total_ht", 0))
        total_ttc_devis = float(devis.get("total_ttc", 0))
        total_ht_acompte = round(total_ht_devis * taux / 100, 2)
        total_ttc_acompte = round(total_ttc_devis * taux / 100, 2)
        
        prestations_api = [Prestation(
            description=f"Acompte {taux}% - {devis.get('titre_projet', devis.get('client_nom', ''))}",
            quantite=1, unite="forfait", prix_unitaire=total_ht_acompte, tva_taux=tva_taux,
        )]
        entreprise_model = Entreprise(
            nom=entreprise.get("nom", ""), gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""), adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""), tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""), logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux, mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        client_model = Client(
            nom=devis.get("client_nom", ""), adresse=devis.get("client_adresse", ""),
            tel=devis.get("telephone_client", ""), email=devis.get("client_email", ""),
        )
        facture_request = FactureRequest(
            entreprise=entreprise_model, client=client_model,
            prestations=prestations_api, tva_taux=tva_taux,
            numero_devis_origine=devis.get("numero_devis", ""),
            is_facture_acompte=True, taux_acompte=taux,
            total_ht=total_ht_acompte, total_ttc=total_ttc_acompte,
            total_ht_devis=total_ht_devis, total_ttc_devis=total_ttc_devis,
        )
        filepath_pdf, numero_facture, _, _ = generer_pdf_facture(facture_request)
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_facture}.pdf")
        filepath_word, _, _, _ = generer_word_facture(facture_request)
        word_url = upload_to_supabase(filepath_word, f"{numero_facture}.docx")
        
        saved = save_facture_to_dashboard(
            entreprise_id=entreprise["id"], devis_id=devis.get("id"),
            numero_facture=numero_facture, client_nom=devis.get("client_nom", ""),
            client_email=devis.get("client_email"), client_telephone=devis.get("telephone_client"),
            client_adresse=devis.get("client_adresse"), titre_projet=devis.get("titre_projet"),
            prestations=[{"description": f"Acompte {taux}%", "quantite": 1, "unite": "forfait", "prix_unitaire": total_ht_acompte}],
            total_ht=total_ht_acompte, total_ttc=total_ttc_acompte,
            pdf_url=pdf_url, word_url=word_url, type_facture="acompte", tva_taux=tva_taux,
        )
        facture_id = saved.get("id", "") if saved else ""
        
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"🧾 Facture {numero_facture}")
        
        send_whatsapp(phone_full, f"""✅ *Facture d'acompte prête !*

🧾 {numero_facture}
💰 Acompte {taux}% : *{fmt_amount(total_ttc_acompte)} TTC*

*1.* 📱 Envoyer WhatsApp
*2.* 📧 Envoyer email
*3.* ✅ Marquer payée
*4.* 🏠 Menu""")
        
        conv["state"] = State.FACTURE_GENERE
        conv["data"]["facture_genere"] = {
            "id": facture_id, "numero_facture": numero_facture,
            "client_nom": devis.get("client_nom", ""), "client_tel": devis.get("telephone_client", ""),
            "client_email": devis.get("client_email", ""), "total_ttc": total_ttc_acompte,
            "pdf_url": pdf_url, "doc_type": "facture",
        }
        save_conv(phone, conv)
    except Exception as e:
        logger.error(f"Erreur génération facture acompte: {e}")
        traceback.print_exc()
        send_whatsapp(phone_full, "Erreur technique 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)


def _generate_facture_finale(phone: str, phone_full: str, conv: Dict):
    """Génère une facture finale (solde)"""
    data = conv.get("data", {})
    devis = data.get("selected_devis", {})
    send_whatsapp(phone_full, "⏳ _Facture finale en cours..._")
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)
        return
    try:
        tva_taux = float(entreprise.get("tva_taux", 20) or 20)
        acompte_ttc_total = 0
        acompte_refs = []
        factures = devis.get("factures", [])
        for f in factures:
            if f.get("type_facture") == "acompte" and f.get("statut") == "payee":
                acompte_ttc_total += float(f.get("total_ttc", 0))
                acompte_refs.append(f.get("numero_facture", ""))
        
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
            nom=entreprise.get("nom", ""), gerant=entreprise.get("gerant", ""),
            siret=entreprise.get("siret", ""), adresse=entreprise.get("adresse", ""),
            cp_ville=entreprise.get("cp_ville", ""), tel=entreprise.get("tel", ""),
            email=entreprise.get("email", ""), logo_url=entreprise.get("logo_url"),
            tva_taux=tva_taux, mention_legale_tva=entreprise.get("mention_legale_tva", ""),
            forme_juridique=entreprise.get("forme_juridique"),
            capital_social=entreprise.get("capital_social", ""),
            rcs=entreprise.get("rcs", ""),
            tva_intracommunautaire=entreprise.get("tva_intracommunautaire", ""),
            couleur_pdf=entreprise.get("couleur_pdf"),
        )
        client_model = Client(
            nom=devis.get("client_nom", ""), adresse=devis.get("client_adresse", ""),
            tel=devis.get("telephone_client", ""), email=devis.get("client_email", ""),
        )
        facture_request = FactureRequest(
            entreprise=entreprise_model, client=client_model,
            prestations=prestations_api, tva_taux=tva_taux,
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
            entreprise_id=entreprise["id"], devis_id=devis.get("id"),
            numero_facture=numero_facture, client_nom=devis.get("client_nom", ""),
            client_email=devis.get("client_email"), client_telephone=devis.get("telephone_client"),
            client_adresse=devis.get("client_adresse"), titre_projet=devis.get("titre_projet"),
            prestations=prestations_data, total_ht=total_ht, total_ttc=total_ttc,
            pdf_url=pdf_url, word_url=word_url, type_facture="complete",
            remise_type=devis.get("remise_type"),
            remise_value=float(devis.get("remise_value", 0) or 0),
            tva_taux=tva_taux, solde_a_payer=reste_a_payer,
        )
        facture_id = saved.get("id", "") if saved else ""
        
        if pdf_url and pdf_url.startswith("http"):
            send_whatsapp_document(phone_full, pdf_url, f"🧾 Facture {numero_facture}")
        
        acompte_text = f"\n💰 Acompte déduit : -{fmt_amount(acompte_ttc_total)}\n💰 *Reste à payer : {fmt_amount(reste_a_payer)}*" if acompte_ttc_total > 0 else ""
        
        send_whatsapp(phone_full, f"""✅ *Facture finale prête !*

🧾 {numero_facture}
💰 Total TTC : {fmt_amount(total_ttc)}{acompte_text}

*1.* 📱 Envoyer WhatsApp
*2.* 📧 Envoyer email
*3.* ✅ Marquer payée
*4.* 🏠 Menu""")
        
        conv["state"] = State.FACTURE_GENERE
        conv["data"]["facture_genere"] = {
            "id": facture_id, "numero_facture": numero_facture,
            "client_nom": devis.get("client_nom", ""), "client_tel": devis.get("telephone_client", ""),
            "client_email": devis.get("client_email", ""),
            "total_ttc": reste_a_payer if acompte_ttc_total > 0 else total_ttc,
            "pdf_url": pdf_url, "doc_type": "facture",
        }
        save_conv(phone, conv)
    except Exception as e:
        logger.error(f"Erreur génération facture finale: {e}")
        traceback.print_exc()
        send_whatsapp(phone_full, "Erreur technique 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)


def _send_email_action(phone: str, phone_full: str, conv: Dict, email: str, avec_signature: bool = False):
    """Envoie un email avec le document"""
    data = conv.get("data", {})
    send_doc = data.get("send_doc", {})
    doc_type = send_doc.get("doc_type", "devis")
    
    send_whatsapp(phone_full, f"📧 _Envoi à {email}..._")
    
    entreprise = get_entreprise(phone)
    if not entreprise:
        send_whatsapp(phone_full, "Entreprise non trouvée 🤔" + NAV_MENU_ONLY)
        reset_conv(phone)
        return
    
    success = False
    if doc_type == "devis":
        success = send_email_devis(email, entreprise, send_doc, avec_signature=avec_signature)
    else:
        success = send_email_facture(email, entreprise, send_doc)
    
    if success:
        doc_id = send_doc.get("id", "")
        if doc_id:
            table = "devis" if doc_type == "devis" else "factures"
            update_document_status(table, doc_id, "envoye" if doc_type == "devis" else "envoyee")
        
        sig_txt = " avec signature ✍️" if avec_signature else ""
        send_whatsapp(phone_full, f"✅ Email envoyé à *{email}*{sig_txt} !\n\n*1.* 📝 Nouveau devis\n*2.* 🏠 Menu")
    else:
        send_whatsapp(phone_full, f"Erreur d'envoi 🤔 Vérifiez l'adresse." + NAV_MENU_ONLY)
    
    reset_conv(phone)


# =============================================================================
# WEBHOOK ENDPOINT
# =============================================================================

@router.post("/webhook/whatsapp")
def whatsapp_webhook(
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
        msg_sid = MessageSid or SmsMessageSid or ""
        if msg_sid:
            now = datetime.now()
            if msg_sid in _processed_sids:
                return {"status": "duplicate"}
            _processed_sids[msg_sid] = now
            old = [s for s, t in _processed_sids.items() if (now - t).total_seconds() > 300]
            for s in old:
                del _processed_sids[s]
        
        phone = From.replace("whatsapp:", "").replace("+", "").strip()
        message = Body.strip()
        button = ButtonPayload or ButtonText or None
        
        logger.info(f"Webhook: phone={phone} msg='{message[:50]}' button={button} media={MediaUrl0}")
        
        handle_message(
            phone=phone, message=message,
            media_url=MediaUrl0, media_type=MediaContentType0,
            button_payload=button,
        )
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        traceback.print_exc()
        return {"status": "error", "detail": str(e)[:100]}


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


# End of whatsapp_handler v9
