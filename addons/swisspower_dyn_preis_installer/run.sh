#!/usr/bin/with-contenv bashio
set -euo pipefail

dest="/config/custom_components/swisspower_dyn_preis"

bashio::log.info "Installing Swisspower Dyn Preis integration to ${dest}."

mkdir -p /config/custom_components
rm -rf "${dest}"
cp -a /root/custom_components/swisspower_dyn_preis "${dest}"

bashio::log.info "Installation complete. You can uninstall this add-on after installation."

sleep 5
