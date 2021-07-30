// Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std::collections::HashSet;
use std::fs;
use std::path::Path;

use toml::value::Table;
use toml::Value;

use super::id::{NameTransform, OptionId};
use super::{ListEdit, ListEditAction, OptionsSource};

#[derive(Clone)]
pub(crate) struct Config {
  config: Value,
}

impl Config {
  pub(crate) fn default() -> Config {
    Config {
      config: Value::Table(Table::new()),
    }
  }

  pub(crate) fn parse<P: AsRef<Path>>(file: P) -> Result<Config, String> {
    let config_contents = fs::read_to_string(&file).map_err(|e| {
      format!(
        "Failed to read config file {}: {}",
        file.as_ref().display(),
        e
      )
    })?;
    let config = config_contents.parse::<Value>().map_err(|e| {
      format!(
        "Failed to parse config file {}: {}",
        file.as_ref().display(),
        e
      )
    })?;
    if config.is_table() {
      Ok(Config { config })
    } else {
      Err(format!(
        "Expected the config file {} to contain a table but contained a {}: {}",
        file.as_ref().display(),
        config.type_str(),
        config
      ))
    }
  }

  pub(crate) fn merged<P: AsRef<Path>>(files: &[P]) -> Result<Config, String> {
    files
      .iter()
      .map(Config::parse)
      .fold(Ok(Config::default()), |acc, parse_result| {
        acc.and_then(|config| parse_result.map(|parsed| config.merge(parsed)))
      })
  }

  fn option_name(id: &OptionId) -> String {
    id.name("_", NameTransform::None)
  }

  fn extract_string_list(option_name: &str, value: &Value) -> Result<Vec<String>, String> {
    if let Some(array) = value.as_array() {
      let mut items = vec![];
      for item in array {
        if let Some(value) = item.as_str() {
          items.push(value.to_owned())
        } else {
          return Err(format!(
            "Expected {} to be an array of strings but given {} containing non string item {}",
            option_name, value, item
          ));
        }
      }
      Ok(items)
    } else {
      Err(format!(
        "Expected {} to be an array but given {}.",
        option_name, value
      ))
    }
  }

  fn get_value(&self, id: &OptionId) -> Option<&Value> {
    self
      .config
      .get(&id.scope())
      .and_then(|table| table.get(Self::option_name(id)))
  }

  pub(crate) fn merge(self, other: Config) -> Config {
    let mut map = self.config.as_table().unwrap().to_owned();
    map.extend(
      other
        .config
        .as_table()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned())),
    );
    Config {
      config: Value::Table(map),
    }
  }
}

impl OptionsSource for Config {
  fn display(&self, id: &OptionId) -> String {
    format!("{}", id)
  }

  fn get_string(&self, id: &OptionId) -> Result<Option<String>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(string) = value.as_str() {
        Ok(Some(string.to_owned()))
      } else {
        Err(format!(
          "Expected {} to be a string but given {}.",
          id, value
        ))
      }
    } else {
      Ok(None)
    }
  }

  fn get_bool(&self, id: &OptionId) -> Result<Option<bool>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(bool) = value.as_bool() {
        Ok(Some(bool))
      } else {
        Err(format!("Expected {} to be a bool but given {}.", id, value))
      }
    } else {
      Ok(None)
    }
  }

  fn get_float(&self, id: &OptionId) -> Result<Option<f64>, String> {
    if let Some(value) = self.get_value(id) {
      if let Some(float) = value.as_float() {
        Ok(Some(float))
      } else {
        Err(format!(
          "Expected {} to be a float but given {}.",
          id, value
        ))
      }
    } else {
      Ok(None)
    }
  }

  fn get_string_list(&self, id: &OptionId) -> Result<Option<Vec<ListEdit<String>>>, String> {
    if let Some(table) = self.config.get(&id.scope()) {
      let option_name = Self::option_name(id);
      let mut list_edits = vec![];
      if let Some(value) = table.get(&option_name) {
        if let Some(sub_table) = value.as_table() {
          if sub_table.is_empty()
            || !sub_table.keys().collect::<HashSet<_>>().is_subset(
              &["add".to_owned(), "remove".to_owned()]
                .iter()
                .collect::<HashSet<_>>(),
            )
          {
            return Err(format!(
              "Expected {} to contain an 'add' element, a 'remove' element or both but found: {:?}",
              option_name, sub_table
            ));
          }
          if let Some(add) = sub_table.get("add") {
            list_edits.push(ListEdit {
              action: ListEditAction::Add,
              items: Self::extract_string_list(&*format!("{}.add", option_name), add)?,
            })
          }
          if let Some(remove) = sub_table.get("remove") {
            list_edits.push(ListEdit {
              action: ListEditAction::Remove,
              items: Self::extract_string_list(&*format!("{}.remove", option_name), remove)?,
            })
          }
        } else {
          list_edits.push(ListEdit {
            action: ListEditAction::Replace,
            items: Self::extract_string_list(&*option_name, value)?,
          });
        }
      }
      if !list_edits.is_empty() {
        return Ok(Some(list_edits));
      }
    }
    Ok(None)
  }
}
