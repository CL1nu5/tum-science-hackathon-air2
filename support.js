(() => {
  class DCLogic {
    constructor(props = {}) {
      this.props = props;
      this.state = {};
      this.__mounted = false;
      this.__renderQueued = false;
    }

    setState(next) {
      const patch = typeof next === 'function' ? next(this.state, this.props) : next;
      this.state = { ...this.state, ...patch };
      this.forceUpdate();
    }

    forceUpdate() {
      if (!this.__mounted || this.__renderQueued) return;
      this.__renderQueued = true;
      queueMicrotask(() => {
        this.__renderQueued = false;
        this.__render?.();
      });
    }
  }

  window.DCLogic = DCLogic;

  const exactExpression = /^\s*\{\{\s*([\s\S]*?)\s*\}\}\s*$/;
  const anyExpression = /\{\{\s*([\s\S]*?)\s*\}\}/g;

  function evaluate(expression, scope) {
    try {
      return Function('scope', `with (scope) { return (${expression}); }`)(scope);
    } catch (error) {
      console.error(`Failed to evaluate "{{ ${expression} }}"`, error);
      return '';
    }
  }

  function interpolate(value, scope) {
    const exact = value.match(exactExpression);
    if (exact) return evaluate(exact[1], scope);
    return value.replace(anyExpression, (_, expression) => {
      const result = evaluate(expression, scope);
      return result == null ? '' : String(result);
    });
  }

  function renderNode(source, scope, documentRef) {
    if (source.nodeType === Node.TEXT_NODE) {
      return [documentRef.createTextNode(interpolate(source.nodeValue || '', scope))];
    }

    if (source.nodeType !== Node.ELEMENT_NODE) return [];

    const tag = source.tagName.toLowerCase();
    if (tag === 'sc-for') {
      const list = interpolate(source.getAttribute('list') || '', scope);
      const name = source.getAttribute('as') || 'item';
      const output = [];
      for (const [index, item] of Array.from(list || []).entries()) {
        const childScope = Object.create(scope);
        childScope[name] = item;
        childScope.$index = index;
        for (const child of source.childNodes) {
          output.push(...renderNode(child, childScope, documentRef));
        }
      }
      return output;
    }

    if (tag === 'sc-if') {
      const visible = interpolate(source.getAttribute('value') || '', scope);
      if (!visible) return [];
      const output = [];
      for (const child of source.childNodes) {
        output.push(...renderNode(child, scope, documentRef));
      }
      return output;
    }

    const element = documentRef.createElement(source.tagName);
    for (const attribute of source.attributes) {
      if (attribute.name.startsWith('hint-')) continue;
      const value = interpolate(attribute.value, scope);
      const name = attribute.name.toLowerCase();

      if (name === 'onclick' && typeof value === 'function') {
        element.__dcClick = value;
        element.onclick = value;
      } else if (name === 'ref' && typeof value === 'function') {
        element.__dcRef = value;
      } else if (value != null && value !== false) {
        element.setAttribute(attribute.name, String(value));
      }
    }

    for (const child of source.childNodes) {
      for (const rendered of renderNode(child, scope, documentRef)) {
        element.appendChild(rendered);
      }
    }
    return [element];
  }

  function syncAttributes(current, fresh) {
    for (const attribute of Array.from(current.attributes)) {
      if (current.tagName === 'CANVAS' && (attribute.name === 'width' || attribute.name === 'height')) {
        continue;
      }
      if (!fresh.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of fresh.attributes) {
      if (current.getAttribute(attribute.name) !== attribute.value) {
        current.setAttribute(attribute.name, attribute.value);
      }
    }
    current.__dcClick = fresh.__dcClick;
    current.onclick = fresh.__dcClick || null;
    current.__dcRef = fresh.__dcRef;
  }

  function morph(current, fresh) {
    if (
      current.nodeType !== fresh.nodeType ||
      (current.nodeType === Node.ELEMENT_NODE && current.tagName !== fresh.tagName)
    ) {
      current.replaceWith(fresh);
      return fresh;
    }

    if (current.nodeType === Node.TEXT_NODE) {
      if (current.nodeValue !== fresh.nodeValue) current.nodeValue = fresh.nodeValue;
      return current;
    }

    syncAttributes(current, fresh);
    const currentChildren = Array.from(current.childNodes);
    const freshChildren = Array.from(fresh.childNodes);
    const common = Math.min(currentChildren.length, freshChildren.length);

    for (let index = 0; index < common; index += 1) {
      morph(currentChildren[index], freshChildren[index]);
    }
    for (let index = currentChildren.length - 1; index >= freshChildren.length; index -= 1) {
      currentChildren[index].remove();
    }
    for (let index = common; index < freshChildren.length; index += 1) {
      current.appendChild(freshChildren[index]);
    }
    return current;
  }

  function callRefs(root) {
    if (root.__dcRef) root.__dcRef(root);
    root.querySelectorAll('*').forEach(element => element.__dcRef?.(element));
  }

  function propsFrom(script) {
    const definition = JSON.parse(script.dataset.props || '{}');
    return Object.fromEntries(
      Object.entries(definition)
        .filter(([name]) => name !== '$preview')
        .map(([name, options]) => [name, options.default])
    );
  }

  function mount() {
    const host = document.querySelector('x-dc');
    const script = document.querySelector('script[data-dc-script]');
    if (!host || !script) return;
    host.style.display = 'block';

    const template = document.createElement('template');
    template.innerHTML = host.innerHTML;

    let Component;
    try {
      Component = Function('DCLogic', `${script.textContent}\nreturn Component;`)(DCLogic);
    } catch (error) {
      console.error('Could not initialize the Design Canvas component.', error);
      return;
    }

    const component = new Component(propsFrom(script));
    let firstRender = true;

    component.__render = () => {
      const values = component.renderVals();
      const scope = Object.assign(Object.create(component), values);
      scope.props = component.props;
      scope.state = component.state;

      const rendered = document.createDocumentFragment();
      for (const child of template.content.childNodes) {
        for (const node of renderNode(child, scope, document)) rendered.appendChild(node);
      }

      if (firstRender) {
        host.replaceChildren(rendered);
        firstRender = false;
      } else {
        const currentChildren = Array.from(host.childNodes);
        const freshChildren = Array.from(rendered.childNodes);
        const common = Math.min(currentChildren.length, freshChildren.length);
        for (let index = 0; index < common; index += 1) {
          morph(currentChildren[index], freshChildren[index]);
        }
        for (let index = currentChildren.length - 1; index >= freshChildren.length; index -= 1) {
          currentChildren[index].remove();
        }
        for (let index = common; index < freshChildren.length; index += 1) {
          host.appendChild(freshChildren[index]);
        }
      }
      callRefs(host);
    };

    component.__mounted = true;
    component.__render();
    window.__dcComponent = component;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
})();
