import { ActivatedRouteSnapshot, DetachedRouteHandle, RouteReuseStrategy } from '@angular/router';

/** Keeps a section alive after the user leaves it, so a half-written form or a loaded list is still there when
 * they come back. A section is created the first time it is opened. Routes opt in with `data: { keepAlive: true }`. */
export class KeepAliveStrategy implements RouteReuseStrategy {
  private readonly kept = new Map<string, DetachedRouteHandle>();

  shouldDetach(route: ActivatedRouteSnapshot): boolean {
    return !!route.data['keepAlive'];
  }

  store(route: ActivatedRouteSnapshot, handle: DetachedRouteHandle | null): void {
    const key = route.routeConfig?.path;
    if (key === undefined) return;
    if (handle) this.kept.set(key, handle);
    else this.kept.delete(key);
  }

  shouldAttach(route: ActivatedRouteSnapshot): boolean {
    return this.kept.has(route.routeConfig?.path ?? '');
  }

  retrieve(route: ActivatedRouteSnapshot): DetachedRouteHandle | null {
    return this.kept.get(route.routeConfig?.path ?? '') ?? null;
  }

  shouldReuseRoute(future: ActivatedRouteSnapshot, current: ActivatedRouteSnapshot): boolean {
    return future.routeConfig === current.routeConfig;
  }
}
