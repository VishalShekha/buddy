# Server Inventory

* **Hostname:** Not specified
* **Hardware:** HP laptop
* **CPU:** Intel Core i7-9750H @ 2.60 GHz (6 cores, 12 threads)
* **RAM:** 16 GB non-ECC (approximately 14 GiB usable by Ubuntu)
* **Storage:** Approximately 1.25 TB total (1 HDD + 1 NVMe SSD)
* **OS + version:** Ubuntu (version not specified)
* **LAN IP (reserved):** Not configured / not specified
* **Tailscale VPN IP:** `100.86.164.60`
* **Public IP type:** Likely CGNAT (using a Vodafone mobile hotspot; not independently verified)
* **ISP + router model:** Vodafone mobile network; smartphone hotspot (phone model not specified)
* **Domain (if any):** Existing domain used for a portfolio website currently deployed through GitHub Actions
* **Backup device:** Hard drive available for backups
* **UPS:** No
* **Date built:** 21-10-2026

## Planned Services

* **Server uptime:** 24/7
* **Public website:** A web interface to access selected self-hosted services
* **Portfolio website:** Host or integrate the existing portfolio website
* **Remote access:** Tailscale VPN
* **Public access method:** To be configured; a tunnel service may be used because a directly accessible public IP is unavailable
* **Backup strategy:** To be configured using the available backup hard drive

## Notes

* The server is an older HP laptop repurposed for home-server use.
* Tailscale provides private network connectivity between authorized devices.
* The Tailscale IP is not a public IP address.
* A reserved LAN IP is not currently configured or confirmed.
* CGNAT is suspected because the server connects through a mobile hotspot, but the public IP configuration has not been verified.
* The existing domain currently hosts the portfolio website through a GitHub Actions deployment workflow. Its DNS and hosting configuration will need to be reviewed before changing how the domain is used.
* A backup hard drive is available, but automated backups have not yet been configured.
* A UPS is not available.
