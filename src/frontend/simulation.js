(() => {
  const MODELS = ['Volocon V3', 'Aria S5', 'Joby J6', 'Lilium L7', 'Midnight M2', 'EHang E8'];
  const SAMPLE_FLEET = [
    { callsign: 'EVX-101', model: 'Volocon V3', speed: 21, battery: 92 },
    { callsign: 'EVX-102', model: 'Aria S5', speed: 19, battery: 76 },
    { callsign: 'EVX-103', model: 'Joby J6', speed: 23, battery: 61 },
    { callsign: 'EVX-104', model: 'Lilium L7', speed: 20, battery: 44 },
    { callsign: 'EVX-105', model: 'Midnight M2', speed: 18, battery: 17, status: 'emergency', emergencyFor: 14 },
    { callsign: 'EVX-106', model: 'EHang E8', speed: 22, battery: 68 },
    { callsign: 'EVX-107', model: 'Volocon V3', speed: 17, battery: 39 },
    { callsign: 'EVX-108', model: 'Aria S5', speed: 24, battery: 83 },
    { callsign: 'EVX-109', model: 'Joby J6', speed: 20, battery: 55 },
    { callsign: 'EVX-110', model: 'Lilium L7', speed: 21, battery: 71 },
    { callsign: 'EVX-111', model: 'Midnight M2', speed: 19, battery: 33 },
    { callsign: 'EVX-112', model: 'EHang E8', speed: 23, battery: 88 },
    { callsign: 'EVX-113', model: 'Volocon V3', speed: 18, battery: 47 },
    { callsign: 'EVX-114', model: 'Aria S5', speed: 22, battery: 64 },
    { callsign: 'EVX-115', model: 'Joby J6', speed: 20, battery: 29 },
    { callsign: 'EVX-116', model: 'Lilium L7', speed: 21, battery: 96, landedFor: 4.5, arrivedVp: 1 },
  ];

  const SEED_EVENTS = [
    ['14:22:00', '⚠ EMERGENCY · EVX-105 low energy 17% · priority route to Klinikum Harlaching', '#d6463a', true],
    ['14:21:44', 'on pad · EVX-116 landed Klinikum Großhadern · stand 2 assigned', '#18926a'],
    ['14:21:31', 'route planned · EVX-104 → Messestadt Ost via sector D4', '#2f6df0'],
    ['14:21:18', 'landing slot · Allianz Arena T-03:48', '#6557d6'],
    ['14:21:06', 'local separation · EVX-107 ⇄ EVX-110', '#2f6df0'],
    ['14:20:52', 'noise spread · sector C3 rebalanced', '#d98a16'],
    ['14:20:39', 'weather advisory · north-east cell drifting 13 m/s', '#5a7fa8'],
  ];

  class Air2Simulation {
    constructor({ vertiports, worldWidth, worldHeight, weather, nav, fleetSize = 24 }) {
      this.vertiports = vertiports;
      this.worldWidth = worldWidth;
      this.worldHeight = worldHeight;
      this.weather = weather;
      this.nav = nav;
      this.time = 0;
      this.emergencyIn = 10;
      this.activeConflicts = {};
      this.pairLog = {};
      this.events = SEED_EVENTS.map(([t, text, color, important]) => ({ t, text, color, important }));
      this.resetFleet(fleetSize);
    }

    resetFleet(fleetSize) {
      const count = Math.max(4, Math.min(24, fleetSize));
      const portCount = this.vertiports.length;
      this.fleetSize = count;
      this.taxis = [];
      this.activeConflicts = {};

      for (let id = 0; id < count; id += 1) {
        const sample = SAMPLE_FLEET[id] || {
          callsign: `EVX-${101 + id}`,
          model: MODELS[id % MODELS.length],
          speed: 17 + (id * 3) % 8,
          battery: 46 + (id * 11) % 49,
        };
        const taxi = {
          id,
          callsign: sample.callsign,
          model: sample.model,
          spd: sample.speed,
          battery: sample.battery,
          status: sample.status || 'normal',
          landed: sample.landedFor || 0,
          emgTimer: sample.emergencyFor || 0,
          conflict: false,
          yielding: false,
          hx: 1,
          hy: 0,
          x: 0,
          y: 0,
          altitude_m: 180 + (id % 5) * 45,
          route: null,
          wp: 0,
          goalVp: undefined,
          originVp: undefined,
          arrivedVp: undefined,
        };

        if (taxi.landed > 0) {
          const portId = sample.arrivedVp != null ? sample.arrivedVp % portCount : id % portCount;
          this.parkAt(taxi, portId);
        } else {
          const origin = id % portCount;
          let destination = (id * 5 + 3) % portCount;
          if (destination === origin) destination = (destination + 1) % portCount;
          const port = this.vertiports[origin];
          Object.assign(taxi, { x: port.x, y: port.y, originVp: origin, goalVp: destination });
          this.assignRoute(taxi, destination);
          this.seedProgress(taxi, 0.12 + (id % 5) * 0.14);
        }
        this.taxis.push(taxi);
      }
    }

    step(dt) {
      this.time += dt;
      for (const taxi of this.taxis) {
        if (taxi.landed > 0) {
          taxi.landed -= dt;
          taxi.battery = Math.min(100, taxi.battery + dt * 5);
          if (taxi.landed <= 0) this.depart(taxi);
          continue;
        }
        this.steer(taxi, dt);
        taxi.battery = Math.max(0, taxi.battery - dt * (0.2 + (taxi.status === 'emergency' ? 0.5 : 0)));
        taxi.x = Math.max(0, Math.min(this.worldWidth, taxi.x));
        taxi.y = Math.max(0, Math.min(this.worldHeight, taxi.y));
      }

      this.resolveConflicts();
      this.updateEmergencies(dt);
      this.updateWeather(dt);
    }

    steer(taxi, dt) {
      if (!taxi.route || taxi.wp >= taxi.route.length) {
        this.arrive(taxi);
        return;
      }

      let target = taxi.route[taxi.wp];
      let dx = target.x - taxi.x;
      let dy = target.y - taxi.y;
      let distance = Math.hypot(dx, dy);
      const lastWaypoint = taxi.wp === taxi.route.length - 1;
      if (distance < (lastWaypoint ? 20 : 30)) {
        if (lastWaypoint) {
          this.arrive(taxi);
          return;
        }
        taxi.wp += 1;
        target = taxi.route[taxi.wp];
        dx = target.x - taxi.x;
        dy = target.y - taxi.y;
        distance = Math.hypot(dx, dy) || 1;
      }

      let desiredX = dx / distance;
      let desiredY = dy / distance;
      for (const other of this.taxis) {
        if (other === taxi || other.landed > 0) continue;
        const ox = taxi.x - other.x;
        const oy = taxi.y - other.y;
        const separation = Math.hypot(ox, oy);
        if (separation > 0.001 && separation < 100) {
          const weight = (100 - separation) / 100;
          desiredX += ox / separation * weight * 0.8;
          desiredY += oy / separation * weight * 0.8;
        }
      }

      const magnitude = Math.hypot(desiredX, desiredY) || 1;
      const desiredAngle = Math.atan2(desiredY / magnitude, desiredX / magnitude);
      const currentAngle = Math.atan2(taxi.hy, taxi.hx);
      let turn = desiredAngle - currentAngle;
      while (turn > Math.PI) turn -= Math.PI * 2;
      while (turn < -Math.PI) turn += Math.PI * 2;
      turn = Math.max(-1.8 * dt, Math.min(1.8 * dt, turn));
      taxi.hx = Math.cos(currentAngle + turn);
      taxi.hy = Math.sin(currentAngle + turn);
      const speed = taxi.spd * (taxi.yielding ? 0.66 : 1);
      taxi.x += taxi.hx * speed * dt;
      taxi.y += taxi.hy * speed * dt;
    }

    resolveConflicts() {
      const seen = {};
      for (const taxi of this.taxis) {
        taxi.conflict = false;
        taxi.yielding = false;
      }
      for (let i = 0; i < this.taxis.length; i += 1) {
        for (let j = i + 1; j < this.taxis.length; j += 1) {
          const a = this.taxis[i];
          const b = this.taxis[j];
          if (a.landed > 0 || b.landed > 0 || Math.hypot(a.x - b.x, a.y - b.y) >= 46) continue;
          a.conflict = true;
          b.conflict = true;
          const yielder = a.battery > b.battery ? a : b;
          yielder.yielding = true;
          const key = `${a.id}-${b.id}`;
          seen[key] = true;
          if (!this.activeConflicts[key]) {
            this.activeConflicts[key] = this.time;
            if (this.time - (this.pairLog[key] || -999) > 20) {
              this.pairLog[key] = this.time;
              this.log(`local separation · ${a.callsign} ⇄ ${b.callsign} · ${yielder.callsign} yields`, '#d98a16');
            }
          }
        }
      }
      for (const key of Object.keys(this.activeConflicts)) {
        if (!seen[key]) delete this.activeConflicts[key];
      }
    }

    updateEmergencies(dt) {
      this.emergencyIn -= dt;
      if (this.emergencyIn <= 0) {
        this.emergencyIn = 16 + Math.random() * 12;
        const candidates = this.taxis.filter(taxi => taxi.status === 'normal' && taxi.landed <= 0);
        const taxi = candidates[Math.floor(Math.random() * candidates.length)];
        if (taxi) {
          taxi.status = 'emergency';
          taxi.battery = Math.min(taxi.battery, 11 + Math.random() * 8);
          taxi.emgTimer = 11 + Math.random() * 6;
          const port = this.nearestPort(taxi);
          this.assignRoute(taxi, port.id);
          this.log(`⚠ EMERGENCY · ${taxi.callsign} low energy ${Math.round(taxi.battery)}% · priority route to ${port.name}`, '#d6463a', true);
        }
      }

      for (const taxi of this.taxis) {
        if (taxi.status !== 'emergency') continue;
        taxi.emgTimer -= dt;
        if (taxi.emgTimer <= 0) {
          taxi.status = 'normal';
          taxi.battery = 42 + Math.random() * 15;
          this.log(`resolved · ${taxi.callsign} secured landing slot, reserve restored`, '#18926a');
        }
      }
    }

    updateWeather(dt) {
      const weather = this.weather;
      if (!weather) return;
      weather.x += weather.vx * dt;
      weather.y += weather.vy * dt;
      if (weather.x > this.worldWidth - 100 || weather.x < 100) {
        weather.vx *= -1;
        this.log('weather cell drift · route advisories updated', '#5a7fa8');
      }
      if (weather.y > this.worldHeight - 100 || weather.y < 100) weather.vy *= -1;
    }

    assignRoute(taxi, destinationId) {
      const destination = this.vertiports[destinationId];
      taxi.goalVp = destinationId;
      taxi.arrivedVp = undefined;
      taxi.route = this.routeTo(taxi.x, taxi.y, destination.x, destination.y);
      taxi.wp = Math.min(1, taxi.route.length - 1);
    }

    routeTo(startX, startY, goalX, goalY) {
      const { cols, rows, cell, blocked } = this.nav;
      const cellIndex = (x, y) => {
        const cx = Math.max(0, Math.min(cols - 1, Math.floor(x / cell)));
        const cy = Math.max(0, Math.min(rows - 1, Math.floor(y / cell)));
        return cy * cols + cx;
      };
      const start = cellIndex(startX, startY);
      const goal = cellIndex(goalX, goalY);
      if (start === goal) return [{ x: startX, y: startY }, { x: goalX, y: goalY }];

      const count = cols * rows;
      const goalCellX = goal % cols;
      const goalCellY = Math.floor(goal / cols);
      const cost = new Float64Array(count).fill(Infinity);
      const estimate = new Float64Array(count).fill(Infinity);
      const previous = new Int32Array(count).fill(-1);
      const queued = new Uint8Array(count);
      const open = [start];
      cost[start] = 0;
      estimate[start] = Math.hypot(start % cols - goalCellX, Math.floor(start / cols) - goalCellY);
      queued[start] = 1;

      let found = false;
      while (open.length) {
        let best = 0;
        for (let i = 1; i < open.length; i += 1) {
          if (estimate[open[i]] < estimate[open[best]]) best = i;
        }
        const current = open.splice(best, 1)[0];
        queued[current] = 0;
        if (current === goal) {
          found = true;
          break;
        }
        const cx = current % cols;
        const cy = Math.floor(current / cols);
        for (let dy = -1; dy <= 1; dy += 1) {
          for (let dx = -1; dx <= 1; dx += 1) {
            if (!dx && !dy) continue;
            const nx = cx + dx;
            const ny = cy + dy;
            if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
            const next = ny * cols + nx;
            if (blocked[next]) continue;
            if (dx && dy && (blocked[cy * cols + nx] || blocked[ny * cols + cx])) continue;
            const nextCost = cost[current] + (dx && dy ? Math.SQRT2 : 1);
            if (nextCost >= cost[next]) continue;
            cost[next] = nextCost;
            previous[next] = current;
            estimate[next] = nextCost + Math.hypot(nx - goalCellX, ny - goalCellY);
            if (!queued[next]) {
              open.push(next);
              queued[next] = 1;
            }
          }
        }
      }

      if (!found) return [{ x: startX, y: startY }, { x: goalX, y: goalY }];
      const cells = [];
      for (let current = goal; current !== -1; current = previous[current]) cells.push(current);
      cells.reverse();
      const points = cells.map(index => ({
        x: (index % cols + 0.5) * cell,
        y: (Math.floor(index / cols) + 0.5) * cell,
      }));
      points[0] = { x: startX, y: startY };
      points[points.length - 1] = { x: goalX, y: goalY };
      return this.simplifyRoute(points);
    }

    simplifyRoute(points) {
      if (points.length <= 2) return points;
      const simplified = [points[0]];
      let current = 0;
      while (current < points.length - 1) {
        let next = points.length - 1;
        while (next > current + 1 && !this.lineIsClear(points[current], points[next])) next -= 1;
        simplified.push(points[next]);
        current = next;
      }
      return simplified;
    }

    lineIsClear(start, end) {
      const { cols, rows, cell, blocked } = this.nav;
      const distance = Math.hypot(end.x - start.x, end.y - start.y);
      const steps = Math.max(1, Math.ceil(distance / (cell * 0.5)));
      for (let step = 0; step <= steps; step += 1) {
        const progress = step / steps;
        const cx = Math.floor((start.x + (end.x - start.x) * progress) / cell);
        const cy = Math.floor((start.y + (end.y - start.y) * progress) / cell);
        if (cx >= 0 && cy >= 0 && cx < cols && cy < rows && blocked[cy * cols + cx]) return false;
      }
      return true;
    }

    seedProgress(taxi, fraction) {
      const route = taxi.route;
      if (!route || route.length < 2) return;
      let total = 0;
      for (let i = 1; i < route.length; i += 1) total += Math.hypot(route[i].x - route[i - 1].x, route[i].y - route[i - 1].y);
      const target = total * Math.max(0, Math.min(0.7, fraction));
      let traversed = 0;
      for (let i = 1; i < route.length; i += 1) {
        const segment = Math.hypot(route[i].x - route[i - 1].x, route[i].y - route[i - 1].y) || 1;
        if (traversed + segment >= target) {
          const progress = (target - traversed) / segment;
          taxi.x = route[i - 1].x + (route[i].x - route[i - 1].x) * progress;
          taxi.y = route[i - 1].y + (route[i].y - route[i - 1].y) * progress;
          taxi.hx = (route[i].x - route[i - 1].x) / segment;
          taxi.hy = (route[i].y - route[i - 1].y) / segment;
          taxi.wp = i;
          return;
        }
        traversed += segment;
      }
    }

    arrive(taxi) {
      const portId = taxi.goalVp != null ? taxi.goalVp : this.nearestPort(taxi).id;
      taxi.landed = 2.4 + Math.random() * 3.2;
      taxi.route = null;
      taxi.wp = 0;
      this.parkAt(taxi, portId);
      if (Math.random() < 0.5) this.log(`on pad · ${taxi.callsign} landed ${this.vertiports[portId].name} · stand assigned`, '#18926a');
    }

    depart(taxi) {
      const origin = taxi.arrivedVp != null ? taxi.arrivedVp : this.nearestPort(taxi).id;
      this.parkAt(taxi, origin);
      let destination = origin;
      while (destination === origin && this.vertiports.length > 1) destination = Math.floor(Math.random() * this.vertiports.length);
      taxi.originVp = origin;
      if (taxi.status === 'emergency') taxi.status = 'normal';
      this.assignRoute(taxi, destination);
      const next = taxi.route[1] || this.vertiports[destination];
      const dx = next.x - taxi.x;
      const dy = next.y - taxi.y;
      const magnitude = Math.hypot(dx, dy) || 1;
      taxi.hx = dx / magnitude;
      taxi.hy = dy / magnitude;
    }

    parkAt(taxi, portId) {
      const port = this.vertiports[portId];
      taxi.arrivedVp = portId;
      taxi.x = port.x;
      taxi.y = port.y;
    }

    nearestPort(taxi) {
      return this.vertiports.reduce((nearest, port) => (
        Math.hypot(port.x - taxi.x, port.y - taxi.y) < Math.hypot(nearest.x - taxi.x, nearest.y - taxi.y)
          ? port
          : nearest
      ));
    }

    log(text, color, important = false) {
      this.events.unshift({ t: this.clock(), text, color, important });
      this.events.length = Math.min(this.events.length, 40);
    }

    clock() {
      const seconds = Math.floor(14 * 3600 + 22 * 60 + this.time);
      const hh = String(Math.floor(seconds / 3600) % 24).padStart(2, '0');
      const mm = String(Math.floor(seconds / 60) % 60).padStart(2, '0');
      const ss = String(seconds % 60).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    }
  }

  window.Air2Simulation = Air2Simulation;
})();
