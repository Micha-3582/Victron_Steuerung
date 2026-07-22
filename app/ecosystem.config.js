// pm2-Konfiguration - gleiches Muster wie Tibbertimer/Chogan auf demselben Server.
// Start:  cd /root/Victron_Steuerung/app && pm2 start ecosystem.config.js && pm2 save
//
// Voraussetzung: Code per git nach /root/Victron_Steuerung geklont und venv
// unter /root/Victron_Steuerung/app/venv angelegt (siehe DEPLOY-Proxmox.md).

module.exports = {
  apps: [
    {
      name: 'victron-steuerung',
      cwd: '/root/Victron_Steuerung/app',
      script: 'webapp.py',
      interpreter: '/root/Victron_Steuerung/app/venv/bin/python',
      env: {
        PORT: 5005,
        // Container laeuft auf UTC. Ohne das zeigen Preis-/Termin-Zeiten
        // verschoben an. Setzt die Zeitzone nur fuer diesen Prozess.
        TZ: 'Europe/Berlin',
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      max_memory_restart: '250M',
      time: true, // Zeitstempel in den pm2-Logs
    },
  ],
};
