#!/usr/bin/env bash
#
# check_gcp.sh — the project-level things an environment needs before
# any of its workloads can start: secrets, buckets, service accounts,
# Workload Identity bindings and the image repository.
#
# EVERY NAME COMES FROM names.sh. Spelling one here would recreate the
# exact failure that file was written to prevent -- two scripts
# agreeing by coincidence until one of them is edited.
#
# Sourced by env_preflight.sh, which has already sourced names.sh and
# report.sh and called agentos_names.

# ── secrets ──────────────────────────────────────────────────────────
#
# EXISTENCE AND A READABLE VERSION, not just existence. A secret with
# no versions describes fine and fails at access time, inside the pod,
# as a mount error with no name attached.
_secret() {
  local name="$1" why="$2" out rc
  out=$(gcloud secrets versions access latest --secret="$name" \
          --project="$PROJECT" 2>&1 >/dev/null); rc=$?
  if [ "$rc" -eq 0 ]; then ok "secret" "$name"; return; fi
  case "$out" in
    *NOT_FOUND*|*was\ not\ found*)
      # WHICH command actually creates this one. Everything here
      # comes from prerequisites.sh except the sqlproxy key, which
      # needs a service account the execution plane creates first.
      case "$name" in
        *execution-sqlproxy-key*)
          miss "secret" "$name — $why" \
               "$(fix_exec_secret "scripts/infra/gke_cluster.sh $ENV_NAME")" ;;
        *)
          miss "secret" "$name — $why" \
               "$(fix_secrets "scripts/infra/db_bootstrap.sh $ENV_NAME")" ;;
      esac ;;
    *PERMISSION_DENIED*|*Permission\ denied*)
      unknown "secret" "$name (cannot read: $out)" ;;
    *) unknown "secret" "$name ($out)" ;;
  esac
}

# ── secrets that must hold a REAL value ──────────────────────────────
#
# A READABLE VERSION IS NOT ENOUGH for these. prerequisites.sh gives
# every unprovided secret a one-space placeholder so Cloud Run will
# start, and the app reads whitespace as "not configured" -- so a blank
# LLM key, Firebase key or App key passes `_secret` above and fails
# later, at login or on the first agent turn, far from here. Read the
# value and require one non-whitespace character. The value itself is
# never printed.
_secret_set() {
  local name="$1" why="$2" fix="$3" val out rc
  # The value from stdout ONLY: a gcloud warning on stderr must not make
  # a blank secret look set.
  val=$(gcloud secrets versions access latest --secret="$name" \
          --project="$PROJECT" 2>/dev/null); rc=$?
  if [ "$rc" -eq 0 ]; then
    if printf '%s' "$val" | grep -q '[^[:space:]]'; then ok "secret" "$name (set)"
    else miss "secret" "$name is BLANK — $why" "$fix"; fi
    return
  fi
  out=$(gcloud secrets versions access latest --secret="$name" \
          --project="$PROJECT" 2>&1 >/dev/null)
  case "$out" in
    *NOT_FOUND*|*was\ not\ found*)
      miss "secret" "$name — $why" \
           "$(fix_secrets "scripts/infra/db_bootstrap.sh $ENV_NAME")" ;;
    # NOT FATAL HERE, and only here. This check reads a secret's CONTENT,
    # and the build account runs preflight without being allowed to read
    # the Firebase or GitHub App key -- correctly: nothing in the build
    # needs them. prerequisites.sh checked their content when it created
    # them, as a person who can. Failing the deploy over a read the
    # account is right not to have was a build that could never pass.
    *PERMISSION_DENIED*|*Permission\ denied*)
      skip "secret" "$name (this account may not read it; prerequisites.sh checks its content)" ;;
    *) unknown "secret" "$name (could not read)" ;;
  esac
}

check_secrets() {
  section "secrets"
  local s
  s="$(agentos_secret gemini-api-key)"
  _secret_set "$s" "Gemini key; blank, every Gemini-backed agent fails its first turn" \
              "printf %s '<value>' | ./set-secret.sh $s"
  s="$(agentos_secret anthropic-api-key)"
  _secret_set "$s" "Anthropic key; blank, every Claude-backed agent fails its first turn" \
              "printf %s '<value>' | ./set-secret.sh $s"
  # UNSUFFIXED in every layout: one GitHub App and one Firebase project
  # serve every environment of an installation, so these two never go
  # through agentos_secret.
  _secret_set "firebase-service-account" "Firebase admin key; blank, nobody can log in" \
              "./set-secret.sh firebase-service-account <path/to/firebase-sa.json>"
  _secret_set "github-app-key" "GitHub App private key; blank, every git action fails" \
              "./set-secret.sh github-app-key <path/to/github-app-key.pem>"
  _secret "$(agentos_secret database-url)"            "the app login"
  _secret "$(agentos_secret database-url-migrate)"    "the DDL login the migrate job uses"
  _secret "$(agentos_secret database-url-conductor)"  "the SIDECAR-form DSN (127.0.0.1:5432), distinct from the Cloud Run socket form"
  _secret "$(agentos_secret execution-db-password)"   "${LEG_ROLE}'s password; pod_main calls leg_dsn() unconditionally"
  _secret "$(agentos_secret execution-sqlproxy-key)"  "GSA key for the pod's cloud-sql-proxy"
  _secret "$(agentos_secret openclaw-gateway-token)"  "ASSISTANT_API_TOKEN; absent, every kernel tool call fails as E_KERNEL_UNREACHABLE"
  _secret "$(agentos_secret credential-encryption-key)" "protects stored credentials"
  _secret "$(agentos_secret agent-token-signing-key)"   "mints agent tokens; sharing one across environments makes them interchangeable"
}

# ── buckets ──────────────────────────────────────────────────────────
check_buckets() {
  section "storage"
  local out rc
  out=$(gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
          --project="$PROJECT" --format='value(name)' 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then ok "bucket" "$ARTIFACT_BUCKET"
  elif printf '%s' "$out" | grep -qiE 'not found|404'; then
    miss "bucket" "$ARTIFACT_BUCKET — conductor writes trees here, verifier reads them" \
         "gcloud storage buckets create gs://${ARTIFACT_BUCKET} --project=${PROJECT} --location=${REGION:-us-central1}"
  else unknown "bucket" "$ARTIFACT_BUCKET ($out)"; fi
}

# ── service accounts and Workload Identity ───────────────────────────
#
# THE BINDING, NOT JUST THE ACCOUNT. gke_cluster.sh created the capture
# GSA, granted it the bucket, wrote the KSA annotation -- and never
# bound the two. Everything described correctly and the pod could not
# get a token. An account that exists unbound is the quiet half.
_wi() {
  local gsa="$1" ksa="$2" out rc member
  member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${ksa}]"
  out=$(gcloud iam service-accounts get-iam-policy "$gsa" \
          --project="$PROJECT" --format=json 2>&1); rc=$?
  # NOT_FOUND IS AN ANSWER. The account is not there, and saying so
  # is the whole point of having three outcomes rather than two --
  # `unknown` means "I could not ask", and here we asked and were told.
  # Reporting a definitive negative as undetermined inflates the count
  # the operator is told to treat as alarming, and spends the
  # distinction this file exists to make.
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *NOT_FOUND*|*Unknown\ service\ account*|*does\ not\ exist*)
        miss "wi-bind" "${gsa%%@*} does not exist" \
             "$(fix_setup "scripts/infra/gke_cluster.sh ${ENV_NAME}")" ;;
      *) unknown "wi-bind" "${ksa} -> ${gsa} ($out)" ;;
    esac
    return
  fi
  if printf '%s' "$out" | grep -qF "$member"; then
    ok "wi-bind" "${ksa} -> ${gsa%%@*}"
  else
    miss "wi-bind" "${ksa} -> ${gsa%%@*} has no workloadIdentityUser binding" \
         "gcloud iam service-accounts add-iam-policy-binding ${gsa} --role=roles/iam.workloadIdentityUser --member='${member}' --project=${PROJECT}"
  fi
}

check_identity() {
  section "service accounts + workload identity"
  local pair
  for pair in "$GSA:$KSA" "$COND_GSA:$COND_KSA" "$VERIFY_GSA:$VERIFY_KSA" \
              "$CAPTURE_GSA:$CAPTURE_KSA" "$COND_GSA:$VERIFIER_CONTROL_KSA"; do
    _wi "${pair%%:*}" "${pair##*:}"
  done
}

# ── the image repository ─────────────────────────────────────────────
check_registry() {
  section "artifact registry"
  local repo="${ENV_SEG}" out rc
  out=$(gcloud artifacts repositories describe "$repo" \
          --location="${REGION:-us-central1}" --project="$PROJECT" \
          --format='value(name)' 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then ok "registry" "$repo"
  elif printf '%s' "$out" | grep -qiE 'not found|404|NOT_FOUND'; then
    miss "registry" "$repo — every image this environment runs is pushed here" \
         "gcloud artifacts repositories create ${repo} --repository-format=docker --location=${REGION:-us-central1} --project=${PROJECT}"
  else unknown "registry" "$repo ($out)"; fi
}
