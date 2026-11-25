import type { MouseEvent } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { prefetchRoute } from "../../pages/routeLoaders";
import { NavigationIcon } from "../NavigationIcon/NavigationIcon";

type MobileBottomNavProps = {
  moreOpen: boolean;
  onOpenMore: (event: MouseEvent<HTMLButtonElement>) => void;
};

export function MobileBottomNav({ moreOpen, onOpenMore }: MobileBottomNavProps) {
  const location = useLocation();
  const moreIsCurrent = navigationItems
    .slice(4)
    .some((item) => item.path === location.pathname);

  return (
    <nav className="mobile-nav" aria-label="移动导航">
      {navigationItems.slice(0, 4).map((item) => (
        <NavLink
          key={item.path}
          to={item.path}
          end
          onMouseEnter={() => prefetchRoute(item.path)}
          onFocus={() => prefetchRoute(item.path)}
          className={({ isActive }) =>
            `mobile-nav__item${isActive ? " mobile-nav__item--active" : ""}`
          }
        >
          <NavigationIcon path={item.path} />
          <span data-text={item.mobileLabel}>{item.mobileLabel}</span>
        </NavLink>
      ))}
      <button
        type="button"
        className={`mobile-nav__item${moreIsCurrent ? " mobile-nav__item--active" : ""}`}
        aria-current={moreIsCurrent ? "page" : undefined}
        aria-expanded={moreOpen}
        aria-controls="more-drawer"
        onClick={onOpenMore}
      >
        <NavigationIcon path="/more" />
        <span data-text="更多">更多</span>
      </button>
    </nav>
  );
}
