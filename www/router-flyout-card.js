class RouterFlyoutCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.image || !config.router_image) {
      throw new Error('image and router_image are required');
    }
    this.config = config;
    if (this._dialogOpen === undefined) {
      this._dialogOpen = false;
      this._refreshing = false;
      this._refreshError = '';
      this._refreshMessage = '';
      this._routerData = {};
      this._lastRefreshAt = null;
      this._lastRouterUpdate = null;
    }
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    if (!this._rendered) {
      this.render();
      this._rendered = true;
      this.updateValues();
      this.updateFeedback();
    } else {
      this.updateValues();
    }
  }

  set hass(hass) {
    this._hass = hass;
    this.updateValues();
    const state = hass.states?.[this.config.timestamp_entity || this.config.entity];
    const reported = state?.last_reported || state?.last_updated;
    if (reported && reported !== this._lastRouterUpdate) {
      this._lastRouterUpdate = reported;
      if (this._dialogOpen && !this._refreshing) void this.loadCurrentData();
    }
  }

  getCardSize() {
    return 7;
  }

  async fetchRouterData() {
    const result = await this._hass.callWS({
      type: 'call_service',
      domain: 'openwrt',
      service: 'get_raw_data',
      service_data: this.config.entry_id ? { entry_id: this.config.entry_id } : {},
      return_response: true,
    });
    if (!result?.response || typeof result.response !== 'object') {
      throw new Error('Home Assistant returned no router data');
    }
    this._routerData = result.response;
    this.updateValues();
  }

  async loadCurrentData() {
    try {
      await this.fetchRouterData();
      this._refreshError = '';
    } catch (error) {
      this._refreshError = `Additional router data could not be loaded: ${error?.message || error}`;
    }
    this.updateFeedback();
  }

  async refreshDetails() {
    if (this._refreshing) return;
    if (!this.config.refresh_entity || !this._hass) {
      this._refreshError = 'Refresh entity is not configured.';
      this.updateFeedback();
      return;
    }

    this._refreshing = true;
    this._refreshError = '';
    this._refreshMessage = '';
    this.updateFeedback();

    let serviceCompleted = false;
    try {
      await this._hass.callService('button', 'press', {
        entity_id: this.config.refresh_entity,
      });
      serviceCompleted = true;
      await this.fetchRouterData();
      this._lastRefreshAt = new Date();
      this._refreshMessage = 'Details updated successfully.';
    } catch (error) {
      this._refreshError = serviceCompleted
        ? `Router refreshed, but details could not be loaded: ${error?.message || error}`
        : `Refresh failed: ${error?.message || error}`;
    } finally {
      this._refreshing = false;
      this.updateValues();
      this.updateFeedback();
    }
  }

  readValue(key, entityKey) {
    const raw = this._routerData?.[key];
    if (raw !== undefined && raw !== null && raw !== '') return raw;

    const entityId = this.config.entity_map?.[entityKey || key];
    const entity = entityId ? this._hass?.states?.[entityId] : undefined;
    if (!entity || ['unknown', 'unavailable'].includes(entity.state)) {
      return null;
    }
    return entity.state;
  }

  formatValue(value, format) {
    if (value === null || value === undefined || value === '') return null;
    const text = String(value).trim();
    const normalized = text.toLowerCase();
    if (format === 'active') {
      if (['1', 'true', 'on', 'enabled', 'up', 'active'].includes(normalized)) return 'Active';
      if (['0', 'false', 'off', 'disabled', 'down', 'inactive'].includes(normalized)) return 'Inactive';
      return text;
    }
    if (format === 'percent') return `${text} %`;
    if (format === 'latency') return /ms$/i.test(text) ? text : `${text} ms`;
    if (format === 'duration') {
      const seconds = Number(value);
      if (!Number.isFinite(seconds) || seconds < 0) return text;
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      return `${days}d ${hours}h ${minutes}m`;
    }
    return text;
  }

  updateValues() {
    if (!this._rendered || !this.shadowRoot) return;

    for (const element of this.shadowRoot.querySelectorAll('[data-value]')) {
      const value = this.readValue(element.dataset.value, element.dataset.entity);
      const display = this.formatValue(value, element.dataset.format) || 'Unavailable';
      element.textContent = display;
      element.parentElement.hidden = display === 'Unavailable';
    }

    for (const section of this.shadowRoot.querySelectorAll('section')) {
      section.hidden = !section.querySelector('.value-row:not([hidden])');
    }

    const statusEntity = this._hass?.states?.[this.config.entity];
    const status = statusEntity?.state || this._routerData?.wan_status || 'unknown';
    const normalized = String(status).toLowerCase();
    const online = ['up', 'online', 'connected', 'on'].includes(normalized);
    const offline = ['down', 'offline', 'disconnected', 'off'].includes(normalized);
    const displayStatus = online ? 'Online' : offline ? 'Offline' : String(status);
    const dot = this.shadowRoot.querySelector('.status-dot');
    dot.classList.toggle('online', online);
    dot.classList.toggle('offline', offline);
    dot.classList.toggle('unknown', !online && !offline);
    dot.title = displayStatus;
    dot.setAttribute('aria-label', displayStatus);

    const titleText = this.config.title || this._routerData?.device_name || 'OpenWrt';
    const labelText = this.shadowRoot.querySelector('.label span');
    if (labelText) labelText.textContent = titleText;
    const dialogTitle = this.shadowRoot.querySelector('#router-title');
    if (dialogTitle) dialogTitle.textContent = titleText;
    this.shadowRoot.querySelector('.label').setAttribute(
      'aria-label',
      `${titleText}, Status ${displayStatus}`,
    );

    const timestamp = this._lastRefreshAt || statusEntity?.last_reported || statusEntity?.last_updated;
    const timestampDate = timestamp instanceof Date ? timestamp : new Date(timestamp);
    this.shadowRoot.querySelector('.meta').textContent =
      `Last updated ${Number.isNaN(timestampDate.getTime()) ? 'Unavailable' : timestampDate.toLocaleString()}`;
  }

  updateFeedback() {
    if (!this._rendered || !this.shadowRoot) return;
    const button = this.shadowRoot.querySelector('.refresh');
    button.disabled = this._refreshing;
    button.textContent = this._refreshing ? 'Refreshing ...' : 'Refresh Details';
    this.shadowRoot.querySelector('.feedback').textContent = this._refreshing
      ? 'Fetching router data ...'
      : this._refreshMessage;
    const error = this.shadowRoot.querySelector('.error');
    error.textContent = this._refreshError;
    error.hidden = !this._refreshError;
  }

  render() {
    const safe = (value) => String(value).replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[char]));
    const sections = [
      ['Device', [
        ['Model', 'model', '', ''],
        ['Hostname', 'hostname', '', ''],
        ['Firmware', 'firmware_version', '', ''],
        ['Kernel', 'kernel_version', '', ''],
        ['Uptime', 'uptime', '', 'duration'],
        ['CPU Load', 'cpu_load', '', ''],
        ['Memory Usage', 'memory_usage_pct', '', 'percent'],
      ]],
      ['Network', [
        ['WAN Status', 'wan_status', 'wan_status', 'active'],
        ['External IP', 'external_ip', '', ''],
        ['Latency', 'latency_ms', '', 'latency'],
      ]],
      ['Wireless & Clients', [
        ['SSIDs', 'wireless_ssids', '', ''],
        ['Channels', 'wireless_channels', '', ''],
        ['Wireless Clients', 'wireless_clients_count', '', ''],
        ['Connected Devices', 'connected_devices_count', 'connected_devices_count', ''],
      ]],
      ['Services', [
        ['VPN Interfaces', 'vpn_interfaces_count', '', ''],
        ['AdBlock', 'adblock_enabled', '', 'active'],
        ['Tailscale', 'tailscale_running', '', 'active'],
        ['Reboot Required', 'reboot_required', '', 'active'],
      ]],
    ];
    const rows = sections.map(([title, fields]) =>
      `<section><h3>${safe(title)}</h3><dl>${fields.map(([label, key, entity, format]) =>
        `<div class="value-row" data-row="${safe(key)}"><dt>${safe(label)}</dt><dd data-value="${safe(key)}" data-entity="${safe(entity)}" data-format="${safe(format)}">Unavailable</dd></div>`
      ).join('')}</dl></section>`
    ).join('');

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);font-family:var(--primary-font-family,Arial,sans-serif)}
        .scene{position:relative;width:100%;overflow:hidden;background:var(--card-background-color,#1c1c1c)}
        .property{display:block;width:100%;height:auto}
        .router{position:absolute;left:30%;top:25%;width:3%;height:auto;max-height:12%;object-fit:contain;transform:translate(-50%,-50%);cursor:pointer;filter:drop-shadow(0 1px 3px #000)}
        .router:focus-visible,button:focus-visible{outline:3px solid var(--primary-color);outline-offset:3px}
        .label{position:absolute;left:31%;top:23%;transform:translateY(-50%);display:flex;align-items:center;gap:8px;padding:4px 8px;border:0;border-radius:4px;background:#000b;color:#fff;font-size:13px;cursor:pointer}
        .status-dot{width:9px;height:9px;flex:none;border-radius:50%;background:#9e9e9e;box-shadow:0 0 4px currentColor}
        .status-dot.online{background:#4caf50;color:#4caf50}.status-dot.offline{background:#f44336;color:#f44336}
        .dialog{position:fixed;inset:0;z-index:1000;display:flex;justify-content:flex-end;align-items:stretch;background:#0009}
        .dialog[hidden]{display:none}
        .panel{box-sizing:border-box;width:min(440px,92vw);height:100%;max-height:100vh;overflow:auto;border-radius:16px 0 0 16px;padding:20px;background:var(--card-background-color,#1c1c1c);box-shadow:0 12px 40px #0008}
        .dialog.opening .panel{animation:flyout-in .2s ease-out}
        @keyframes flyout-in{from{transform:translateX(100%)}to{transform:translateX(0)}}
        header{display:flex;align-items:center;justify-content:space-between;gap:12px}h2{font-size:20px;margin:0}button{font:inherit;color:inherit;background:transparent;border:0;cursor:pointer}.close{font-size:24px;padding:4px 10px}
        section{margin-top:16px}section[hidden],.value-row[hidden]{display:none}h3{margin:0 0 8px;color:var(--primary-color);font-size:16px}dl{margin:0;display:grid;grid-template-columns:1fr 1fr;gap:4px 16px}dl>div{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--divider-color,#444)}dt{opacity:.8}dd{margin:0;text-align:right;font-weight:500;overflow-wrap:anywhere}.meta{margin:16px 0 8px;color:var(--secondary-text-color);font-size:12px}.feedback,.error{font-size:13px}.feedback{color:var(--secondary-text-color)}.error{color:var(--error-color,#f44336)}.link{display:inline-block;padding:10px 14px;border-radius:6px;background:var(--primary-color);color:var(--text-primary-color,#fff);text-decoration:none;font-weight:600}.refresh{display:block;margin-top:12px;padding:10px 14px;border-radius:6px;background:var(--primary-color);color:var(--text-primary-color,#fff);font-weight:600}.refresh:disabled{opacity:.6;cursor:wait}
        @media(max-width:480px){.panel{width:100vw;padding:16px;border-radius:0}dl{grid-template-columns:1fr}.label{font-size:10px;padding:3px 5px}}
      </style>
      <div class="scene">
        <img class="property" src="${safe(this.config.image)}" alt="Floorplan">
        <img class="router" src="${safe(this.config.router_image)}" alt="OpenWrt Router" role="button" tabindex="0" aria-label="Show router details">
        <button class="label" type="button" aria-label="OpenWrt, Status unknown"><span>OpenWrt</span><span class="status-dot unknown" role="img"></span></button>
      </div>
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="router-title" hidden>
        <div class="panel"><header><h2 id="router-title">OpenWrt</h2><button class="close" type="button" aria-label="Close">×</button></header>
        ${rows}<p class="meta">Last updated Unavailable</p><button class="refresh" type="button">Refresh Details</button><p class="feedback" role="status"></p><p class="error" role="alert" hidden></p><a class="link" href="${safe(this.config.status_url || 'http://openwrt.lan/cgi-bin/luci/')}" target="_blank" rel="noopener noreferrer">Open LuCI in new tab ↗</a></div>
      </div>`;

    const dialog = this.shadowRoot.querySelector('.dialog');
    const router = this.shadowRoot.querySelector('.router');
    const open = () => {
      if (this._dialogOpen) return;
      this._dialogOpen = true;
      dialog.hidden = false;
      dialog.classList.add('opening');
      this.shadowRoot.querySelector('.close').focus();
      void this.refreshDetails();
    };
    const close = () => {
      this._dialogOpen = false;
      dialog.classList.remove('opening');
      dialog.hidden = true;
      router.focus();
    };
    this.shadowRoot.querySelector('.panel').addEventListener('animationend', () => {
      dialog.classList.remove('opening');
    });
    router.addEventListener('click', open);
    router.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });
    this.shadowRoot.querySelector('.label').addEventListener('click', open);
    this.shadowRoot.querySelector('.close').addEventListener('click', close);
    this.shadowRoot.querySelector('.refresh').addEventListener('click', () => {
      void this.refreshDetails();
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) close();
    });
    this.shadowRoot.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !dialog.hidden) close();
    });
    this.updateValues();
    this.updateFeedback();
  }
}

customElements.define('router-flyout-card', RouterFlyoutCard);
