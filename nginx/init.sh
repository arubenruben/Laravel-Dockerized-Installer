#!/bin/sh

# Exit on any error and treat unset variables as error
set -euo pipefail

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Function to validate required environment variables
validate_env_vars() {
    local missing_vars=""
    
    if [ -z "${PHP_FPM_HOST:-}" ]; then
        missing_vars="$missing_vars PHP_FPM_HOST"
    fi
    
    if [ -z "${PHP_FPM_PORT:-}" ]; then
        missing_vars="$missing_vars PHP_FPM_PORT"
    fi
    
    if [ -n "$missing_vars" ]; then
        log "ERROR: Missing required environment variables:$missing_vars"
        log "Please set these variables before starting the container"
        exit 1
    fi
    
    log "Required environment variables validated successfully"
    log "PHP_FPM_HOST=${PHP_FPM_HOST}"
    log "PHP_FPM_PORT=${PHP_FPM_PORT}"
}

# Validate environment variables
log "Validating environment variables..."
validate_env_vars

# Check if template file exists
TEMPLATE_FILE="/etc/nginx/nginx.conf.template"
CONFIG_FILE="/etc/nginx/nginx.conf"

if [ ! -f "$TEMPLATE_FILE" ]; then
    log "ERROR: Template file $TEMPLATE_FILE not found"
    exit 1
fi

log "Substituting environment variables in Nginx configuration..."

# Use envsubst with explicit variable list for security
envsubst '${PHP_FPM_HOST},${PHP_FPM_PORT}' < "$TEMPLATE_FILE" > "$CONFIG_FILE"

if [ ! -f "$CONFIG_FILE" ]; then
    log "ERROR: Failed to generate configuration file $CONFIG_FILE"
    exit 1
fi

log "Configuration file generated successfully"

# Test nginx configuration
log "Testing Nginx configuration..."
if nginx -t -c "$CONFIG_FILE"; then
    log "Nginx configuration test passed"
else
    log "ERROR: Nginx configuration test failed"
    exit 1
fi

# Check if nginx entrypoint exists and is executable
ENTRYPOINT_SCRIPT="/docker-entrypoint.sh"
if [ -f "$ENTRYPOINT_SCRIPT" ]; then
    log "Running Nginx entrypoint script..."
    if [ -x "$ENTRYPOINT_SCRIPT" ]; then
        sh "$ENTRYPOINT_SCRIPT"
    else
        log "WARNING: $ENTRYPOINT_SCRIPT exists but is not executable"
        chmod +x "$ENTRYPOINT_SCRIPT"
        sh "$ENTRYPOINT_SCRIPT"
    fi
else
    log "WARNING: Nginx entrypoint script $ENTRYPOINT_SCRIPT not found, skipping..."
fi

log "Starting Nginx..."
exec "$@"