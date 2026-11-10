// The parts of the Hermes dashboard plugin SDK the Argus dashboard uses (type-check only; nothing imports
// this file at runtime). Mirrors hermes-agent web/src/plugins/sdk.d.ts (SDK_CONTRACT_VERSION 1.1.0) and
// registry.ts::exposePluginSDK; React is narrowed to the calls dist/index.js makes, with React's signatures.

declare namespace Hermes {
  type ReactNode = ReactElement | string | number | boolean | null | undefined;

  interface ReactElement {
    readonly type: unknown;
    readonly props: unknown;
    readonly key: string | null;
  }

  /** A function component without props (what register / registerSlot receive). */
  type Component = () => ReactNode;

  interface RefObject<T> {
    current: T;
  }

  type SetStateAction<S> = S | ((prev: S) => S);
  type Dispatch<A> = (value: A) => void;
  type EffectCallback = () => void | (() => void);

  interface React {
    createElement(
      type: string | Component,
      props: { readonly [prop: string]: unknown } | null,
      ...children: ReactNode[]
    ): ReactElement;
  }

  interface Hooks {
    useState<S>(initialState: S | (() => S)): [S, Dispatch<SetStateAction<S>>];
    useEffect(effect: EffectCallback, deps?: readonly unknown[]): void;
    useRef<T>(initialValue: T): RefObject<T>;
  }

  /** JSON fetch with the host's auth; throws `Error("<status>: <body>")` on non-2xx. */
  type FetchJSON = (
    url: string,
    init?: RequestInit,
    options?: { allowUnauthorized?: boolean },
  ) => Promise<unknown>;

  /** window.__HERMES_PLUGIN_SDK__ */
  interface PluginSDK {
    readonly sdkVersion: string;
    React: React;
    hooks: Hooks;
    fetchJSON: FetchJSON;
  }

  /** window.__HERMES_PLUGINS__ */
  interface PluginRegistry {
    register(name: string, component: Component): void;
    registerSlot(plugin: string, slot: string, component: Component): void;
  }
}

interface Window {
  __HERMES_PLUGIN_SDK__?: Hermes.PluginSDK;
  __HERMES_PLUGINS__?: Hermes.PluginRegistry;
  /** Path prefix the dashboard is served under ("" at the root). */
  __HERMES_BASE_PATH__?: string;
}
