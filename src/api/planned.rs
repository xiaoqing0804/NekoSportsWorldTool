//! 一次定时跑步的明确参数及提交前约束；不修改请求时钟或账户身高。
use crate::track::{geom::MET_PER_DEG_LAT, model::Track};
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub scheduled_ms: i64,
    pub start_ms: i64,
    pub school_window_start_ms: i64,
    pub school_window_end_ms: i64,
    pub distance_m: f64,
    pub duration_s: i64,
    /// 仅供步长合理性检查，不是海拔或协议字段。
    pub height_cm: f64,
    pub cadence_spm: i64,
    pub seed: u64,
    pub anchor_lat_bd: f64,
    pub anchor_lon_bd: f64,
    /// 限定本次点位区域；不能替代服务端校园围栏。
    pub max_radius_m: f64,
}

impl Plan {
    pub fn validate(&self) -> Result<(), String> {
        if !(1000.0..=2000.0).contains(&self.distance_m)
            || !(300..=1200).contains(&self.duration_s)
            || !(100.0..=220.0).contains(&self.height_cm)
            || !(140..=200).contains(&self.cadence_spm)
            || !(50.0..=1500.0).contains(&self.max_radius_m)
        {
            return Err("计划参数超出距离、时长、身高、步频或区域限制".into());
        }
        crate::location::Coordinate::new(self.anchor_lat_bd, self.anchor_lon_bd, 0.0)?;
        let pace = self.duration_s as f64 * 1000.0 / self.distance_m;
        let stride = self.distance_m / (self.cadence_spm as f64 * self.duration_s as f64 / 60.0);
        if !(180.0..=600.0).contains(&pace)
            || !(0.3..=1.0).contains(&(stride / (self.height_cm / 100.0)))
        {
            return Err("计划配速或步长与参数不匹配".into());
        }
        let stop = self
            .start_ms
            .checked_add(self.duration_s * 1000)
            .ok_or("计划时间溢出")?;
        let deadline = self
            .scheduled_ms
            .checked_add(300_000)
            .ok_or("预约时间溢出")?;
        if self.school_window_start_ms <= 0
            || self.start_ms < self.school_window_start_ms
            || stop > self.scheduled_ms
            || deadline > self.school_window_end_ms
        {
            return Err("跑步时间或提交时间不在配置的学校时段内".into());
        }
        Ok(())
    }

    pub fn validate_at(&self, now: i64) -> Result<(), String> {
        self.validate()?;
        if now < self.scheduled_ms || now >= self.scheduled_ms + 300_000 {
            return Err("不在预约后的五分钟执行窗口内，未提交".into());
        }
        Ok(())
    }

    pub fn distance_from_anchor(&self, lat: f64, lon: f64) -> f64 {
        let dy = (lat - self.anchor_lat_bd) * MET_PER_DEG_LAT;
        let dx = (lon - self.anchor_lon_bd) * 111_320.0 * self.anchor_lat_bd.to_radians().cos();
        dx.hypot(dy)
    }

    pub fn params(&self) -> super::flow::RunParams {
        super::flow::RunParams {
            dist: self.distance_m,
            dur: self.duration_s,
            start_ms: self.start_ms,
            face_check: 0,
            manual_altitude: None,
            manual_altitude_range: None,
            seed: self.seed,
            route_mode: crate::track::generate_road::RouteMode::Legacy,
        }
    }

    pub fn apply_cadence(&self, track: &mut Track) -> Result<(), String> {
        if track.totalSteps <= 0 {
            return Err("轨迹缺少有效步数".into());
        }
        let target = (self.cadence_spm as f64 * track.totalTime as f64 / 60.0).round();
        let scale = target / track.totalSteps as f64;
        for point in &mut track.locations {
            point.steps = (point.steps as f64 * scale).round() as i64;
        }
        // 提交体、十秒窗口、分段和 OBS 均读取同一份最终累计步数。
        crate::track::stats::rebuild(track);
        track.validate_consistency()?;
        Ok(())
    }

    pub fn build_track(
        &self,
        policy: &super::policy::PolicyInfo,
        points: &[serde_json::Value],
        fences: &[Vec<(f64, f64)>],
    ) -> Result<Track, String> {
        self.validate()?;
        if policy.min_distance as f64 > self.distance_m {
            return Err("服务器最低距离超过本次计划，未提交".into());
        }
        if points.is_empty() || super::points::points_bd(points).len() != points.len() {
            return Err("缺少有效服务器打卡点，未提交".into());
        }
        let route = super::points::route_points(&policy.must_points, points);
        if route
            .iter()
            .any(|&(lat, lon)| self.distance_from_anchor(lat, lon) > self.max_radius_m)
        {
            return Err("服务器点位越出本次锚点区域，未提交".into());
        }
        let mut error = "无法生成满足计划的路线".to_string();
        // 旧生成器使用固定经度米数；先转换到其内部平面，再还原真实 BD 坐标。
        // 否则益阳纬度下，轨迹的实际东西向距离会比累计距离大约多一成。
        let longitude_scale =
            111_320.0 * self.anchor_lat_bd.to_radians().cos() / crate::track::geom::MET_PER_DEG_LNG;
        let projected: Vec<_> = route
            .iter()
            .map(|&(lat, lon)| {
                (
                    lat,
                    self.anchor_lon_bd + (lon - self.anchor_lon_bd) * longitude_scale,
                )
            })
            .collect();
        // 仅在本地选择环线起点；网络提交始终只有一次。
        for offset in 0..64 {
            let mut track = crate::track::generator::build(
                self.distance_m,
                self.duration_s,
                self.seed.wrapping_add(offset),
                (self.anchor_lat_bd, self.anchor_lon_bd),
                self.start_ms,
                &projected,
            );
            for point in &mut track.locations {
                point.gLng =
                    self.anchor_lon_bd + (point.gLng - self.anchor_lon_bd) / longitude_scale;
            }
            track.startLongitude = track.locations[0].gLng;
            self.apply_cadence(&mut track)?;
            match self.validate_track(&track, policy, points, fences) {
                Ok(()) => return Ok(track),
                Err(reason) => error = reason,
            }
        }
        Err(error)
    }

    pub fn validate_track(
        &self,
        track: &Track,
        policy: &super::policy::PolicyInfo,
        points: &[serde_json::Value],
        fences: &[Vec<(f64, f64)>],
    ) -> Result<(), String> {
        track.validate_consistency()?;
        if track.startTime != self.start_ms
            || track.totalTime != self.duration_s
            || (track.totalDistance - self.distance_m).abs() > 0.5
            || track.totalDistance + 0.5 < policy.min_distance as f64
        {
            return Err("最终轨迹与预约参数或服务器最低距离不一致".into());
        }
        let cadence = track.totalSteps as f64 * 60.0 / track.totalTime as f64;
        if (cadence - self.cadence_spm as f64).abs() > 0.1 {
            return Err("最终步频与预约参数不一致".into());
        }
        if track
            .locations
            .iter()
            .any(|p| self.distance_from_anchor(p.gLat, p.gLng) > self.max_radius_m)
        {
            return Err("最终轨迹越出本次锚点区域，未提交".into());
        }
        let mut required = policy.must_points.clone();
        for point in points {
            let flag = point.get("isFixed").ok_or("点位缺少固定点标记")?;
            let fixed = match (flag.as_i64(), flag.as_str(), flag.as_bool()) {
                (Some(0), _, _) | (_, Some("0"), _) | (_, _, Some(false)) => false,
                (Some(1), _, _) | (_, Some("1"), _) | (_, _, Some(true)) => true,
                _ => return Err("无法识别点位的固定点标记".into()),
            };
            if fixed {
                required.push(super::fence::point_xy(point).ok_or("固定点坐标无效")?);
            }
        }
        if required
            .iter()
            .any(|p| !crate::track::wire::passes_point(track, *p))
        {
            return Err("2 公里内的最终轨迹未经过策略必经点或固定打卡点，未提交".into());
        }
        if !fences.is_empty() {
            super::school_roads::validate_fences(fences)?;
            let polygons: Vec<Vec<_>> = fences
                .iter()
                .map(|ring| {
                    ring.iter()
                        .map(|&(lat, lon)| route_planner::Coord::new(lon, lat))
                        .collect()
                })
                .collect();
            let coord =
                |p: &crate::track::model::GenPoint| route_planner::Coord::new(p.gLng, p.gLat);
            if !track
                .locations
                .iter()
                .all(|p| route_planner::boundary::contains(&polygons, coord(p)))
                || !track.locations.windows(2).all(|p| {
                    route_planner::boundary::segment_inside(&polygons, coord(&p[0]), coord(&p[1]))
                })
            {
                return Err("最终轨迹或连线越出服务器校园围栏，未提交".into());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn plan() -> Plan {
        Plan {
            scheduled_ms: 1_800_003_600_000,
            start_ms: 1_800_002_940_000,
            school_window_start_ms: 1_800_000_000_000,
            school_window_end_ms: 1_800_007_200_000,
            distance_m: 2000.0,
            duration_s: 600,
            height_cm: 156.0,
            cadence_spm: 168,
            seed: 7,
            anchor_lat_bd: 28.54686,
            anchor_lon_bd: 112.39125,
            max_radius_m: 900.0,
        }
    }
    fn policy() -> super::super::policy::PolicyInfo {
        super::super::policy::PolicyInfo {
            timestamp: 1,
            policy: 1,
            min_distance: 1000,
            valid_time: 0,
            must_points: vec![],
        }
    }
    fn track(p: &Plan) -> Track {
        crate::track::generator::build(
            p.distance_m,
            p.duration_s,
            p.seed,
            (p.anchor_lat_bd, p.anchor_lon_bd),
            p.start_ms,
            &[
                (28.54686, 112.39125),
                (28.54786, 112.39225),
                (28.54686, 112.39225),
            ],
        )
    }
    #[test]
    fn wrong_time_and_future_record_fail_closed() {
        let mut p = plan();
        assert!(p.validate_at(p.scheduled_ms).is_ok());
        assert!(p.validate_at(p.scheduled_ms - 1).is_err());
        assert!(p.validate_at(p.scheduled_ms + 300_000).is_err());
        p.start_ms = p.scheduled_ms;
        assert!(p.validate().is_err());
    }

    #[test]
    fn two_km_at_seven_minutes_sixteen_seconds_keeps_872_seconds() {
        let mut p = plan();
        p.duration_s = 2 * (7 * 60 + 16);
        p.start_ms = p.scheduled_ms - 60_000 - p.duration_s * 1000;
        p.validate_at(p.scheduled_ms).unwrap();
        let mut t = track(&p);
        p.apply_cadence(&mut t).unwrap();
        p.validate_track(&t, &policy(), &[], &[]).unwrap();
        assert_eq!(t.totalTime, 872);
        assert_eq!(t.totalSteps, (168.0_f64 * 872.0 / 60.0).round() as i64);
        assert_eq!(t.startTime + t.totalTime * 1000, p.scheduled_ms - 60_000);
    }
    #[test]
    fn cadence_updates_every_derived_counter_without_changing_altitude() {
        let p = plan();
        let mut t = track(&p);
        let altitudes: Vec<_> = t.locations.iter().map(|q| q.bdA).collect();
        p.apply_cadence(&mut t).unwrap();
        assert_eq!(t.totalSteps, 1680);
        assert_eq!(
            t.stepsPerTenSec.iter().map(|w| w.value as i64).sum::<i64>(),
            1680
        );
        assert_eq!(
            altitudes,
            t.locations.iter().map(|q| q.bdA).collect::<Vec<_>>()
        );
        p.validate_track(&t, &policy(), &[], &[]).unwrap();
    }
    #[test]
    fn distant_track_and_missed_fixed_points_are_rejected() {
        let p = plan();
        let mut t = track(&p);
        p.apply_cadence(&mut t).unwrap();
        let point = serde_json::json!({"lat":28.54,"lon":112.39,"isFixed":1});
        assert!(p.validate_track(&t, &policy(), &[point], &[]).is_err());
        t.locations[3].gLat += 0.1;
        assert!(p.validate_track(&t, &policy(), &[], &[]).is_err());
    }

    #[test]
    fn planned_geometry_uses_actual_latitude_and_fences_reject_outside_segments() {
        let p = plan();
        let points: Vec<_> = [
            (28.54676, 112.38825),
            (28.54696, 112.38825),
            (28.54696, 112.39425),
            (28.54676, 112.39425),
        ]
        .into_iter()
        .map(|(lat, lon)| serde_json::json!({"lat":lat,"lon":lon,"isFixed":0}))
        .collect();
        let t = p.build_track(&policy(), &points, &[]).unwrap();
        let length: f64 = t
            .locations
            .windows(2)
            .map(|pair| {
                let dy = (pair[1].gLat - pair[0].gLat) * MET_PER_DEG_LAT;
                let dx =
                    (pair[1].gLng - pair[0].gLng) * 111_320.0 * p.anchor_lat_bd.to_radians().cos();
                dx.hypot(dy)
            })
            .sum();
        assert!((length - p.distance_m).abs() < 80.0, "geometry={length}");
        let fence = vec![vec![
            (28.546, 112.390),
            (28.548, 112.390),
            (28.548, 112.393),
            (28.546, 112.393),
        ]];
        assert!(p.validate_track(&t, &policy(), &points, &fence).is_err());
    }
}
